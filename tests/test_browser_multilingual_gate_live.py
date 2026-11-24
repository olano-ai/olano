"""The approval gate on real pages in Spanish, Chinese, Japanese and Korean,
in a real headless Chromium (skipped without playwright/Chromium).

A JavaScript send button ("Enviar", "发送", "送信", "보내기") must stop for a
person; a pay button on a POST form must stay ``protected`` even under a
per-site rule that lets sending through; a button with no recognized word on a
checkout page must stop; and a button whose label is an icon but whose code
name says "send" must stop. Every "stop" here is the tool refusing, because a
direct tool call has nobody to ask (tests/test_browser_fast_actions_live.py
covers the same seam in English).
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


# lang → (send label, pay label)
LANGS = {
    "es": ("Enviar", "Pagar ahora"),
    "zh": ("发送", "立即付款"),
    "ja": ("送信", "支払う"),
    "ko": ("보내기", "결제하기"),
}


def _page(lang: str) -> str:
    send, pay = LANGS[lang]
    return f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{lang}</title></head>
<body>
<textarea id="body"></textarea>
<button id="send" onclick="document.getElementById('out').textContent='SENT'">{send}</button>
<button id="icon" class="msg-form__send-button" aria-label="&#x27A4;"
  onclick="document.getElementById('out').textContent='ICON-SENT'">&#x27A4;</button>
<p id="out">idle</p>
<form method="post" action="/{lang}/order"><button id="pay" type="submit">{pay}</button></form>
</body></html>"""


_CHECKOUT = """<!doctype html><html lang="sw"><head><meta charset="utf-8"><title>Malipo</title></head>
<body><button id="lipa" onclick="document.getElementById('out').textContent='PAID'">Lipa</button>
<p id="out">idle</p></body></html>"""


@pytest.fixture(scope="module")
def site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, text: str) -> None:
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith("/checkout"):
                return self._send(_CHECKOUT)
            self._send(_page(path.strip("/").split("/")[0] or "es"))

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            self.server.posts.append(self.path)  # type: ignore[attr-defined]
            self._send("<p>ok</p>")

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


def _url(site, path: str) -> str:
    return f"http://127.0.0.1:{site.server_address[1]}/{path}"


def _ref(snapshot: str, name: str) -> str:
    match = re.search(rf'button "{re.escape(name)}" \[ref=(e\d+)\]', snapshot)
    assert match, f"button {name!r} not in snapshot:\n{snapshot[:2000]}"
    return match.group(1)


def _out(sessions) -> str:
    from olano.browser import loop as browser_loop

    async def read():
        return await sessions.get("_default").page.inner_text("#out")

    return browser_loop.run_sync(read(), timeout_s=30)


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_a_javascript_send_button_asks_in_every_language(lang, site, browser_on):
    from olano.tools.browser import browser_click, browser_navigate

    send, _ = LANGS[lang]
    page = _invoke(browser_navigate, {"url": _url(site, lang)})
    out = _invoke(browser_click, {"ref": _ref(page, send)})
    assert out.startswith("Not done:"), out
    assert _out(browser_on) == "idle"


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_an_icon_button_named_send_in_the_code_asks(lang, site, browser_on):
    from olano.tools.browser import browser_click, browser_navigate

    page = _invoke(browser_navigate, {"url": _url(site, lang)})
    out = _invoke(browser_click, {"ref": _ref(page, "➤")})
    assert out.startswith("Not done:"), out
    assert _out(browser_on) == "idle"


@pytest.mark.parametrize("lang", sorted(LANGS))
def test_a_send_rule_lets_send_through_but_never_pay(lang, site, browser_on, monkeypatch):
    from olano.browser import auto_approve
    from olano.config.agent_config import BrowserAgentConfig
    from olano.tools.browser import browser_click, browser_navigate

    rules = BrowserAgentConfig.model_validate(
        {"auto_approve": [{"site": "shop.example.com", "actions": ["send", "submit", "signup"]}]}
    ).auto_approve
    monkeypatch.setattr(auto_approve, "rules_for", lambda agent: list(rules))
    monkeypatch.setattr(
        auto_approve, "host_of", lambda url: "shop.example.com" if "127.0.0.1" in url else ""
    )
    send, pay = LANGS[lang]

    page = _invoke(browser_navigate, {"url": _url(site, lang)})
    out = _invoke(browser_click, {"ref": _ref(page, send)})
    assert "Approved automatically" in out, out
    assert _out(browser_on) == "SENT"

    page = _invoke(browser_navigate, {"url": _url(site, lang)})
    out = _invoke(browser_click, {"ref": _ref(page, pay)})
    assert out.startswith("Not done:"), out  # money always asks, whatever the rules say
    assert site.posts == []


def test_any_button_on_a_checkout_page_asks_whatever_it_says(site, browser_on, monkeypatch):
    from olano.browser import auto_approve
    from olano.config.agent_config import BrowserAgentConfig
    from olano.tools.browser import browser_click, browser_navigate

    rules = BrowserAgentConfig.model_validate(
        {"auto_approve": [{"site": "shop.example.com", "actions": ["send", "submit", "signup"]}]}
    ).auto_approve
    monkeypatch.setattr(auto_approve, "rules_for", lambda agent: list(rules))
    monkeypatch.setattr(
        auto_approve, "host_of", lambda url: "shop.example.com" if "127.0.0.1" in url else ""
    )
    page = _invoke(browser_navigate, {"url": _url(site, "checkout/hatua-2")})
    out = _invoke(browser_click, {"ref": _ref(page, "Lipa")})  # Swahili "Pay": in no list
    assert out.startswith("Not done:"), out
    assert _out(browser_on) == "idle"
