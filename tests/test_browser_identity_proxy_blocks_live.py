"""Live: what a site sees of an agent's browser, the proxy, and block pages.

Against a real headless Chromium through the real tools, because each property
here is something only the browser decides: the headers it sends, whether a
context really goes through a proxy (and signs in to it), and what an agent is
told when a site turns it away, and whether the next attempt reaches the site
at all.
"""

from __future__ import annotations

import base64
import http.server
import threading
import urllib.request

import pytest

from tests.test_tools_browser_live import (  # noqa: F401
    _chromium_available,
    _invoke,
    as_agent,
    browser_on,
)

pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="playwright/chromium not installed"
)

_PROBE_PAGE = """<!doctype html><title>probe</title><body><p id=js>pending</p><script>
const d = navigator.userAgentData;
document.getElementById('js').textContent = JSON.stringify({
  webdriver: navigator.webdriver,
  brands: d ? d.brands.map(b => b.brand) : null,
  platform: d ? d.platform : null,
  ua: navigator.userAgent,
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
});
</script></body>"""

_REDDIT_BLOCK = """<!doctype html><title>Blocked</title><body>
<h1>You've been blocked by network security.</h1>
<p>To continue, log in to your Reddit account or use your developer token</p>
<p>If you think you've been blocked by mistake, file a ticket below and we'll look into it.</p>
<button id=retry>Try again</button></body>"""

# Cloudflare's interstitial, reduced to what matters here: completing the
# check posts back to the page's own address, which answers with a clearance
# cookie and a redirect to the page itself.
_CHALLENGE = """<!doctype html><title>Just a moment...</title><body>
<h1>Verifying you are human. This may take a few seconds.</h1>
<form method=post action="/challenge?__cf_chl_tk=t"><button id=verify>Verify</button></form>
</body>"""

# The same interstitial when the browser passes: the test runs in the page,
# sets the clearance cookie and reloads, with nobody touching anything.
_SELF_CLEARING_CHALLENGE = """<!doctype html><title>Just a moment...</title><body>
<h1>Verifying you are human. This may take a few seconds.</h1>
<script>setTimeout(() => { document.cookie = "cf_clearance=ok; path=/"; location.reload(); }, 1000);
</script></body>"""

_RECAPTCHA_FORM = """<!doctype html><title>Sign in</title><body>
<form><input name=email><button>Continue</button></form>
<p>This site is protected by reCAPTCHA and the Google Privacy Policy and Terms of Service apply.</p>
</body>"""


@pytest.fixture(scope="module")
def site():
    hits: list[tuple[str, dict[str, str]]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            hits.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            status, body = 200, "<title>Home</title><h1>home</h1><img src='/pix.png'>"
            if self.path.startswith("/probe"):
                body = _PROBE_PAGE
            elif self.path.startswith("/blocked"):
                status, body = 403, _REDDIT_BLOCK
            elif self.path.startswith("/signin"):
                body = _RECAPTCHA_FORM
            elif self.path.startswith(("/challenge", "/passes")):
                cleared = "cf_clearance=ok" in self.headers.get("Cookie", "")
                if cleared:
                    body = "<title>Listings</title><h1>listings</h1>"
                elif self.path.startswith("/passes"):
                    status, body = 403, _SELF_CLEARING_CHALLENGE
                else:
                    status, body = 403, _CHALLENGE
            elif self.path.startswith("/link"):
                # Served as localhost: a different site linking to the blocked one.
                port = self.server.server_port
                body = f"<title>Links</title><a id=l href='http://127.0.0.1:{port}/blocked'>go</a>"
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            hits.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Set-Cookie", "cf_clearance=ok; Path=/")
            self.send_header("Location", "/challenge")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.hits = hits  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def _url(server, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def _probe(extracted: str) -> dict:
    """The JSON the probe page wrote, out of browser_extract's untrusted fence."""
    import json

    start, end = extracted.index("{"), extracted.rindex("}")
    return json.loads(extracted[start : end + 1])


def _proxy_server(user: str, password: str):
    """A forwarding HTTP proxy that demands Basic auth, and records what it relayed."""
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    relayed: list[str] = []
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    class Proxy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get("Proxy-Authorization") != expected:
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="test"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            relayed.append(self.path)
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ("proxy-authorization", "proxy-connection", "host")
            }
            try:
                with direct.open(urllib.request.Request(self.path, headers=headers)) as up:
                    status, data, ctype = up.status, up.read(), up.headers.get("Content-Type", "")
            except urllib.error.HTTPError as err:
                status, data, ctype = err.code, err.read(), err.headers.get("Content-Type", "")
            self.send_response(status)
            self.send_header("Content-Type", ctype or "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    server.relayed = relayed  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def loopback_through_proxy(monkeypatch):
    """Chromium never proxies loopback unless told to (``<-loopback>``); the
    test site is on loopback, so the parsed proxy gains that bypass rule."""
    from olano.browser import launch

    real = launch.parse_proxy

    def parse(raw):
        proxy = real(raw)
        if proxy:
            proxy["bypass"] = "<-loopback>"
        return proxy

    monkeypatch.setattr(launch, "parse_proxy", parse)


@pytest.fixture(autouse=True)
def _no_remembered_blocks(monkeypatch):
    from olano.browser import blockpage

    # Long enough for the self-clearing check (1 s), short for the ones that never clear.
    monkeypatch.setattr(blockpage, "INTERSTITIAL_WAIT_S", 4.0)
    blockpage.forget()
    yield
    blockpage.forget()


def test_a_site_sees_a_browser_that_does_not_contradict_itself(site, browser_on, tmp_path):  # noqa: F811
    """No automation marker, no HeadlessChrome brand in the request or in
    navigator, and a user agent that names the platform the hints name."""
    from olano.tools.browser import browser_extract, browser_navigate

    with as_agent("alice", tmp_path):
        result = _invoke(browser_navigate, {"url": _url(site, "/probe")})
        assert result.startswith("# probe"), result
        seen = _probe(_invoke(browser_extract, {"selector": "#js"}))
    headers = [h for p, h in site.hits if p == "/probe"][-1]
    assert "Headless" not in headers["user-agent"], headers["user-agent"]
    assert "HeadlessChrome" not in headers.get("sec-ch-ua", ""), headers
    assert "Chromium" in headers.get("sec-ch-ua", ""), headers
    # Chromium weights the list itself; a pre-weighted value came out ";q=0.9;q=0.9".
    assert headers["accept-language"] == "en-US,en;q=0.9", headers
    # Chrome's reduced user agent, never the build number.
    assert ".0.0.0 Safari" in headers["user-agent"], headers["user-agent"]
    assert seen["webdriver"] is False
    assert "HeadlessChrome" not in seen["brands"]
    assert "Headless" not in seen["ua"]
    platform_hint = headers.get("sec-ch-ua-platform", "").strip('"')
    assert platform_hint and platform_hint == seen["platform"], headers
    ua_platform = {"Linux": "Linux", "macOS": "Mac OS X", "Windows": "Windows"}[platform_hint]
    assert ua_platform in seen["ua"]


def test_timezone_setting_is_what_the_page_reads(site, browser_on, tmp_path):  # noqa: F811
    from olano.config.system_settings import set_browser_settings
    from olano.tools.browser import browser_extract, browser_navigate

    set_browser_settings({"timezone": "Asia/Tokyo"})
    with as_agent("alice", tmp_path):
        _invoke(browser_navigate, {"url": _url(site, "/probe")})
        text = _invoke(browser_extract, {"selector": "#js"})
    seen = _probe(text)
    assert seen["tz"] == "Asia/Tokyo"


def test_the_browser_goes_through_the_proxy_and_signs_in_to_it(
    site,
    browser_on,  # noqa: F811
    loopback_through_proxy,
    monkeypatch,
    tmp_path,
):
    from olano.tools.browser import browser_navigate

    first = _proxy_server("agent", "s3cret")
    second = _proxy_server("other", "pw")
    try:
        monkeypatch.setenv(
            "BROWSER_PROXY_URL", f"http://agent:s3cret@127.0.0.1:{first.server_port}"
        )
        with as_agent("alice", tmp_path):
            result = _invoke(browser_navigate, {"url": _url(site, "/"), "wait_for": "load"})
            assert result.startswith("# Home"), result
            # The page and what it loads, both through the signed-in proxy.
            assert any(path.endswith("/") for path in first.relayed), first.relayed
            assert any(path.endswith("/pix.png") for path in first.relayed), first.relayed

            # A changed proxy applies on the next call: the session is reopened.
            monkeypatch.setenv(
                "BROWSER_PROXY_URL", f"http://other:pw@127.0.0.1:{second.server_port}"
            )
            assert _invoke(browser_navigate, {"url": _url(site, "/")}).startswith("# Home")
            assert second.relayed, "the reopened session did not use the new proxy"

            monkeypatch.setenv("BROWSER_PROXY_URL", "ftp://127.0.0.1:21")
            refused = _invoke(browser_navigate, {"url": _url(site, "/")})
            assert "BROWSER_PROXY_URL" in refused and "ftp" in refused, refused
    finally:
        first.shutdown()
        second.shutdown()


def test_a_block_page_stops_the_agent_and_the_next_attempt_never_reaches_the_site(
    site,
    browser_on,  # noqa: F811
    tmp_path,
):
    from olano.browser import blockpage
    from olano.tools.browser import browser_click, browser_navigate

    def site_hits(prefix: str) -> int:
        return sum(1 for p, _ in site.hits if p.startswith(prefix))

    with as_agent("alice", tmp_path):
        before = site_hits("/blocked")
        result = _invoke(browser_navigate, {"url": _url(site, "/blocked")})
        assert result.startswith(blockpage.MARKER), result
        assert "blocked by network security" in result
        assert "do not open it again" in result
        assert "API" not in result
        assert site_hits("/blocked") == before + 1

        # Again, or another address on the same site: refused before sending.
        again = _invoke(browser_navigate, {"url": _url(site, "/blocked")})
        other = _invoke(browser_navigate, {"url": _url(site, "/")})
        for text in (again, other):
            assert text.startswith(f"{blockpage.MARKER}: Not opened."), text
        assert site_hits("/blocked") == before + 1

        # Clicking "Try again" on the block page is trying again.
        clicked = _invoke(browser_click, {"selector": "#retry"})
        assert clicked.startswith(f"{blockpage.MARKER}: Not done."), clicked

        # A link from another site into the blocked one is stopped at the
        # request, before it is sent.
        other = f"http://localhost:{site.server_address[1]}/link"
        assert _invoke(browser_navigate, {"url": other}).startswith("# Links")
        followed = _invoke(browser_click, {"selector": "#l"})
        assert followed.startswith(f"{blockpage.MARKER}: Not opened."), followed
        assert site_hits("/blocked") == before + 1

        # Handing the live browser back (a person may have signed in) lifts it.
        browser_on.get("alice").controller = "user"
        from olano.browser import loop as browser_loop

        browser_loop.run_sync(browser_on.set_controller("alice", "agent"), timeout_s=30)
        assert _invoke(browser_navigate, {"url": _url(site, "/")}).startswith("# Home")


def test_a_person_in_control_gets_past_the_check_that_stopped_the_agent(
    site,
    browser_on,  # noqa: F811
    tmp_path,
):
    """The block memory stops the agent, never the person it asked for help.

    Completing the check navigates the page (a post back to its own address,
    then a reload), and that navigation used to be aborted as a retry of the
    remembered site: the person saw Chromium's "blocked" page instead of the
    site. An agent re-reading the page while they work must not re-arm it.
    """
    from olano.browser import blockpage, human_check
    from olano.browser import loop as browser_loop
    from olano.tools.browser import browser_navigate, browser_snapshot

    with as_agent("alice", tmp_path):
        result = _invoke(browser_navigate, {"url": _url(site, "/challenge")})
        assert result.startswith(human_check.MARKER), result

        browser_loop.run_sync(browser_on.set_controller("alice", "user"), timeout_s=30)
        sess = browser_on.get("alice")
        # The agent reads the page while the person is still looking at the check.
        assert _invoke(browser_snapshot, {}).startswith(blockpage.MARKER)
        assert blockpage.recent("alice", _url(site, "/")) is not None

        async def _complete_the_check() -> tuple[str, str]:
            async with sess.page.expect_navigation(url="**/challenge"):
                await sess.page.click("#verify")
            return sess.page.url, await sess.page.title()

        url, title = browser_loop.run_sync(_complete_the_check(), timeout_s=30)
        assert (url, title) == (_url(site, "/challenge"), "Listings")

        # Typed into the address bar, or reloaded: the person's own navigation.
        async def _reload() -> str:
            await sess.page.reload()
            return await sess.page.title()

        assert browser_loop.run_sync(_reload(), timeout_s=30) == "Listings"

        browser_loop.run_sync(browser_on.set_controller("alice", "agent"), timeout_s=30)
        assert _invoke(browser_snapshot, {}).startswith("# Listings")


def test_a_check_that_lets_the_browser_through_is_waited_out(
    site,
    browser_on,  # noqa: F811
    tmp_path,
):
    """Cloudflare's "Just a moment..." passes an ordinary browser on its own a
    few seconds later. It used to be reported as a block the moment it showed,
    and the block memory then aborted the check's own reload."""
    from olano.browser import blockpage
    from olano.tools.browser import browser_navigate, browser_navigate_and_screenshot

    with as_agent("alice", tmp_path):
        result = _invoke(browser_navigate, {"url": _url(site, "/passes")})
        assert result.startswith("# Listings"), result
        assert blockpage.recent("alice", _url(site, "/")) is None

        shot = _invoke(
            browser_navigate_and_screenshot,
            {"url": _url(site, "/passes"), "save_path": str(tmp_path / "p.png")},
        )
        assert shot.startswith("Screenshot saved:") and "Title: Listings" in shot, shot


def test_a_check_that_does_not_clear_is_still_a_block(
    site,
    browser_on,  # noqa: F811
    tmp_path,
):
    import time

    from olano.browser import blockpage, human_check
    from olano.tools.browser import browser_navigate

    with as_agent("alice", tmp_path):
        started = time.monotonic()
        result = _invoke(browser_navigate, {"url": _url(site, "/challenge")})
        # A check a person passes; with no screen for one, the agent moves on.
        assert result.startswith(human_check.MARKER), result
        assert "no screen" in result
        assert time.monotonic() - started >= blockpage.INTERSTITIAL_WAIT_S
        again = _invoke(browser_navigate, {"url": _url(site, "/challenge")})
        assert again.startswith(f"{blockpage.MARKER}: Not opened."), again


def test_a_sign_in_form_that_mentions_recaptcha_is_not_a_block(site, browser_on, tmp_path):  # noqa: F811
    from olano.browser import blockpage
    from olano.tools.browser import browser_navigate

    with as_agent("alice", tmp_path):
        result = _invoke(browser_navigate, {"url": _url(site, "/signin")})
    assert not result.startswith(blockpage.MARKER), result
    assert result.startswith("# Sign in")


def test_a_refused_capture_does_not_stop_the_signed_in_session(site, browser_on, tmp_path):  # noqa: F811
    """A one-shot capture is signed out: its refusal stops further captures of
    the site, not the agent's own (possibly signed-in) browser."""
    from olano.browser import blockpage
    from olano.tools.browser import browser_navigate, browser_navigate_and_screenshot

    with as_agent("alice", tmp_path):
        shot = _invoke(
            browser_navigate_and_screenshot,
            {"url": _url(site, "/blocked"), "save_path": str(tmp_path / "b.png")},
        )
        assert shot.startswith(blockpage.MARKER), shot
        again = _invoke(
            browser_navigate_and_screenshot,
            {"url": _url(site, "/"), "save_path": str(tmp_path / "c.png")},
        )
        assert again.startswith(f"{blockpage.MARKER}: Not opened."), again
        assert _invoke(browser_navigate, {"url": _url(site, "/")}).startswith("# Home")
