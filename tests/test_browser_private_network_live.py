"""A page on a private address can talk to its own server through the guard.

Found benchmarking an agent against an intranet replica on 172.17.0.1 with
``LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK=1``: the page loaded, and its own
``fetch('/api', {method: 'POST'})`` died with "the request client is not a
secure context and the resource is in more-private address space `local`".
The guard fulfilled every navigation from ``route.fetch``, and Chromium files
a fulfilled document under the public address space whatever host it came
from, so Local Network Access refused the page every request to its own
origin. The guard no longer fulfills any document (Chromium fetches every
page, for its TLS fingerprint as much as for this); the first test keeps
the cause pinned so nobody brings a fulfilling route back.

Loopback never showed it (``localhost`` is a secure context), which is why the
existing live suite on 127.0.0.1 stayed green: these tests need a local-network
address of this machine (the default route's source address, or the docker
bridge gateway) and skip without one.

Run just these:  uv run pytest tests/test_browser_private_network_live.py -q
"""

from __future__ import annotations

import http.server
import ipaddress
import socket
import threading
from functools import lru_cache

import pytest

# Chromium's ``local`` address space: RFC 1918, carrier-grade NAT, link-local.
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
)


def is_local_address(raw: str) -> bool:
    try:
        return any(ipaddress.ip_address(raw) in net for net in _LOCAL_NETWORKS)
    except ValueError:
        return False


@lru_cache(maxsize=1)
def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
            return True
    except Exception:
        return False


def _lan_address() -> str | None:
    """An IPv4 address of this machine in Chromium's local space, if any.

    Narrower than "not global" on purpose: a sandbox's default route can sit
    on TEST-NET (192.0.2.0/24), which Python calls non-global and Chromium
    calls public, so nothing would reproduce there."""
    candidates: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET: picks a route, sends nothing
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    candidates.append("172.17.0.1")  # the docker bridge gateway
    for cand in candidates:
        if not is_local_address(cand):
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((cand, 0))
        except OSError:
            continue
        return cand
    return None


_LAN = _lan_address()

pytestmark = [
    pytest.mark.skipif(not _chromium_available(), reason="playwright/chromium not installed"),
    pytest.mark.skipif(_LAN is None, reason="no local-network address on this machine"),
]

_APP = b"""<!doctype html><title>Intranet app</title><div id=out>pending</div><script>
fetch('/api', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{"q":1}'})
  .then(r => r.text()).then(t => { document.getElementById('out').textContent = 'ok:' + t; })
  .catch(e => { document.getElementById('out').textContent = 'error:' + e; });
</script>"""


@pytest.fixture(scope="module")
def lan_site():
    """An intranet app on the LAN address. Bound to every interface, so the
    same server also answers as ``localhost`` (the refused redirect target)."""
    posts: list[bytes] = []
    landed: list[str] = []
    port_box: dict[str, int] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: bytes, status: int = 200, headers: dict | None = None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/redir":
                target = f"http://localhost:{port_box['port']}/landed"
                self._send(b"", status=302, headers={"Location": target})
            elif self.path == "/landed":
                landed.append(self.headers.get("Host", ""))
                self._send(b"<title>landed</title>landed")
            else:
                self._send(_APP)

        def do_POST(self):
            posts.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            self._send(b"pong")

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    port_box["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.posts = posts  # type: ignore[attr-defined]
    server.landed = landed  # type: ignore[attr-defined]
    server.base = f"http://{_LAN}:{port_box['port']}"  # type: ignore[attr-defined]
    yield server
    server.shutdown()


async def _browser(pw):
    from olano.browser import launch

    return await pw.chromium.launch(
        headless=True, args=launch.base_launch_args(), **launch.launch_kwargs()
    )


async def _app_result(page, url: str) -> tuple[str, list[str]]:
    console: list[str] = []
    page.on("console", lambda msg: console.append(msg.text))
    await page.goto(url)
    await page.wait_for_function("document.getElementById('out').textContent !== 'pending'")
    return str(await page.text_content("#out")), console


async def test_a_fulfilled_lan_document_cannot_fetch_its_own_origin(lan_site):
    """The cause, pinned without the guard: the same page works when Chromium
    loads it and fails once a route fulfills it. This is one of the two
    reasons the guard never fulfills a document."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await _browser(pw)
        try:
            plain = await browser.new_context()
            out, _ = await _app_result(await plain.new_page(), f"{lan_site.base}/")
            assert out == "ok:pong", out

            fulfilled = await browser.new_context()

            async def fulfill_documents(route, request):
                if request.is_navigation_request():
                    await route.fulfill(response=await route.fetch(max_redirects=0))
                else:
                    await route.continue_()

            await fulfilled.route("**/*", fulfill_documents)
            out, console = await _app_result(await fulfilled.new_page(), f"{lan_site.base}/")
            assert out.startswith("error:"), out
            assert any("more-private address space" in line for line in console), console
        finally:
            await browser.close()


async def test_guarded_lan_page_posts_to_its_own_origin(lan_site, monkeypatch):
    """With private network access allowed, an intranet app's own fetch POST
    reaches its server through the guard."""
    from playwright.async_api import async_playwright

    from olano.browser.guard import GuardPolicy, GuardState, clear_dns_cache, install_guards

    monkeypatch.setenv("LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK", "1")
    clear_dns_cache()
    lan_site.posts.clear()
    async with async_playwright() as pw:
        browser = await _browser(pw)
        try:
            context = await browser.new_context()
            state = GuardState(policy=GuardPolicy.capture())
            assert state.policy.block_private is False
            await install_guards(context, state)
            out, console = await _app_result(await context.new_page(), f"{lan_site.base}/")
        finally:
            await browser.close()
    assert out == "ok:pong", (out, console)
    assert lan_site.posts == [b'{"q":1}']
    assert state.drain() == []


async def test_lan_redirect_to_a_refused_host_is_stopped_before_it_is_sent(lan_site):
    """Chromium follows a LAN document's redirect itself; ``guard_page``
    pauses the hop inside the browser and fails it, so the refused host never
    receives it, and a page without that interception is still discarded
    after the fact."""
    from playwright.async_api import async_playwright

    from olano.browser.guard import (
        GuardPolicy,
        GuardState,
        clear_dns_cache,
        guard_page,
        install_guards,
        landed_refusal,
    )

    clear_dns_cache()
    lan_site.landed.clear()
    async with async_playwright() as pw:
        browser = await _browser(pw)
        try:
            context = await browser.new_context()
            state = GuardState(policy=GuardPolicy(allow=[_LAN], block_private=False))
            await install_guards(context, state)
            page = await context.new_page()
            assert await guard_page(context, page, state) is not None
            try:
                await page.goto(f"{lan_site.base}/redir")
            except Exception as exc:
                assert "ERR_BLOCKED_BY_CLIENT" in str(exc), exc
            assert not page.url.startswith("http://localhost:"), page.url
            assert lan_site.landed == []
            (entry,) = [e for e in state.drain() if e["redirect"]]
            assert entry["sent"] is False and "localhost" in entry["url"]

            # The belt: a page nothing intercepted lands, and is discarded unread.
            plain = await context.new_page()
            await plain.goto(f"{lan_site.base}/redir")
            assert plain.url.startswith("http://localhost:"), plain.url
            reason = await landed_refusal(plain, state)
            assert reason and "localhost" in reason, reason
            assert plain.url == "about:blank"
            assert next(e for e in state.drain() if e["redirect"])["sent"] is True
        finally:
            await browser.close()
