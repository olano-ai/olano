"""The browser's debug trace (olano/browser/debug_trace.py): what it keeps and what it concludes.

The live half plays Cloudflare against a real Chromium: a "Just a moment..." page
whose script fetches a challenge endpoint that sets a ``Partitioned``
``cf_clearance`` cookie (the shape the real one has) and reloads. The site
either honours the pass or answers every load with another challenge, and
``/cdn-cgi/trace`` reports a fixed or a changing exit address, so each cause the
trace exists to tell apart is produced for real and read back.
"""

from __future__ import annotations

import http.server
import itertools
import threading

import pytest

from olano.browser import debug_trace
from olano.browser.debug_trace import analyze, clean_url, cookie_names, set_cookies

# ── what is kept ──────────────────────────────────────────────────────────────


def test_urls_lose_query_values_and_token_segments():
    url = (
        "https://www.example.com/cdn-cgi/challenge-platform/h/b/fo/"
        "a4e99155bbe9d6d1a4e99155bbe9d6d1/x?__cf_chl_rt_tk=SECRET&ray=1"
    )
    assert clean_url(url) == (
        "https://www.example.com/cdn-cgi/challenge-platform/h/b/fo/…/x?__cf_chl_rt_tk&ray"
    )
    assert "SECRET" not in clean_url(url)


def test_cookies_are_names_and_attributes_never_values():
    assert cookie_names("a=1; cf_clearance=SECRET; b") == ["a", "cf_clearance", "b"]
    parsed = set_cookies(
        "cf_clearance=SECRET; path=/; domain=.example.com; Secure; HttpOnly; "
        "SameSite=None; Partitioned\n__cf_bm=X; Max-Age=1800"
    )
    assert parsed[0] == {
        "name": "cf_clearance",
        "path": "/",
        "domain": ".example.com",
        "secure": True,
        "httponly": True,
        "samesite": "None",
        "partitioned": True,
    }
    assert parsed[1] == {"name": "__cf_bm", "max-age": "1800"}
    assert "SECRET" not in repr(parsed)


# ── what it concludes ─────────────────────────────────────────────────────────


def _issued(t: float = 1.0) -> dict:
    return {"t": t, "kind": "response", "type": "xhr", "cookies_set": [{"name": "cf_clearance"}]}


def _page(t: float, status: int, mitigated: str = "") -> dict:
    return {
        "t": t,
        "kind": "response",
        "type": "document",
        "main_frame": True,
        "status": status,
        "mitigated": mitigated,
    }


def _nav(t: float, sent: list[str]) -> dict:
    return {"t": t, "kind": "request", "main_frame": True, "cookies_sent": sent}


def _codes(trace: dict) -> list[str]:
    return [f["code"] for f in analyze(trace)["findings"]]


def test_a_pass_sent_and_refused_is_a_rejection():
    events = [
        _page(0, 403, "challenge"),
        _issued(),
        _nav(2, ["cf_clearance"]),
        _page(3, 403, "challenge"),
    ]
    assert _codes({"events": events}) == ["clearance_rejected"]


def test_chromiums_account_beats_playwrights_header_list():
    """Playwright's headers omit partitioned cookies; Chromium says it was sent."""
    considered = {
        "t": 2.1,
        "kind": "cookies_considered",
        "cookies": [{"name": "cf_clearance", "sent": True, "blocked": []}],
    }
    events = [_page(0, 403, "challenge"), _issued(), _nav(2, ["__cf_bm"]), considered]
    events.append(_page(3, 403, "challenge"))
    assert _codes({"events": events}) == ["clearance_rejected"]


def test_a_pass_left_out_names_chromiums_reason():
    considered = {
        "t": 2.1,
        "kind": "cookies_considered",
        "cookies": [{"name": "cf_clearance", "sent": False, "blocked": ["ThirdPartyPhaseout"]}],
    }
    events = [_page(0, 403, "challenge"), _issued(), _nav(2, []), considered]
    events.append(_page(3, 403, "challenge"))
    findings = analyze({"events": events})["findings"]
    assert [f["code"] for f in findings] == ["clearance_not_sent"]
    assert "ThirdPartyPhaseout" in findings[0]["text"]


def test_only_the_sites_own_page_counts_as_passing():
    """Cloudflare's box is a document too; a 200 for it is not the site."""
    box = {"t": 3, "kind": "response", "type": "document", "main_frame": False, "status": 200}
    events = [_page(0, 403, "challenge"), _issued(), _nav(2, ["cf_clearance"]), box]
    events.append(_page(4, 403, "challenge"))
    assert _codes({"events": events}) == ["clearance_rejected"]
    events = [_page(0, 403, "challenge"), _issued(), _nav(2, ["cf_clearance"]), _page(3, 200)]
    assert _codes({"events": events}) == ["passed"]


def test_no_pass_and_a_changing_exit_and_refusals():
    trace = {
        "events": [_page(0, 403, "challenge")],
        "exit_samples": [{"ip": "1.1.1.1"}, {"ip": "2.2.2.2"}, {"ip": "1.1.1.1"}],
        "guard_refusals": [{"url": "https://challenges.cloudflare.com/x"}],
    }
    analysis = analyze(trace)
    assert [f["code"] for f in analysis["findings"]] == [
        "exit_changes",
        "guard_refused",
        "clearance_not_issued",
    ]
    assert analysis["exit_addresses"] == ["1.1.1.1", "2.2.2.2"]


# ── live, against a fake Cloudflare ───────────────────────────────────────────

from tests.test_tools_browser_live import (  # noqa: F401
    _chromium_available,
    _invoke,
    as_agent,
    browser_on,
)

live = pytest.mark.skipif(not _chromium_available(), reason="playwright/chromium not installed")

_CHECK = """<!doctype html><title>Just a moment...</title><body>
<h1>Performing security verification</h1>
<script>fetch('/cdn-cgi/challenge-platform/h/b/fo/a4e99155bbe9d6d1a4e99155bbe9d6d1')
  .then(() => setTimeout(() => location.reload(), 300));</script></body>"""


@pytest.fixture(scope="module")
def cloudflare():
    state = {"honour": True, "rotate": False}
    ips = itertools.cycle(["203.0.113.7", "203.0.113.8"])

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: str, headers: dict | None = None) -> None:
            data = body.encode()
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/cdn-cgi/trace"):
                ip = next(ips) if state["rotate"] else "203.0.113.7"
                self._send(200, f"fl=1\nip={ip}\nloc=SG\ncolo=SIN\n")
            elif self.path.startswith("/cdn-cgi/challenge-platform"):
                self._send(
                    200,
                    "ok",
                    {
                        "Set-Cookie": "cf_clearance=SECRET; Path=/; Secure; HttpOnly; "
                        "SameSite=None; Partitioned"
                    },
                )
            elif self.path.startswith("/plain"):
                self._send(200, "<title>Plain</title>")
            elif state["honour"] and "cf_clearance=" in self.headers.get("Cookie", ""):
                self._send(200, "<title>Listings</title><h1>listings</h1>")
            else:
                self._send(403, _CHECK, {"cf-mitigated": "challenge"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.state = state  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    from olano.browser import blockpage, human_check

    monkeypatch.setattr(blockpage, "INTERSTITIAL_WAIT_S", 3.0)
    monkeypatch.setattr(human_check, "SECONDS_PER_MINUTE", 3.0)
    monkeypatch.setattr(debug_trace, "EXIT_SAMPLE_EVERY_S", 0.0)
    blockpage.forget()
    yield
    blockpage.forget()


def _url(server, path: str) -> str:
    return f"http://localhost:{server.server_address[1]}{path}"


def _trace(cloudflare, browser_on, tmp_path, *, honour: bool, rotate: bool):  # noqa: F811
    from olano.browser import loop as browser_loop
    from olano.tools.browser import browser_navigate

    cloudflare.state.update(honour=honour, rotate=rotate)
    with as_agent("alice", tmp_path):
        assert _invoke(browser_navigate, {"url": _url(cloudflare, "/plain")}).startswith("# Plain")
        sess = browser_on.get("alice")

        async def _start():
            debug_trace.start(sess, "by hand")

        browser_loop.run_sync(_start(), timeout_s=10)
        result = _invoke(browser_navigate, {"url": _url(cloudflare, "/")}, deadline_s=90)

        async def _stop():
            return debug_trace.stop(sess)

        return result, browser_loop.run_sync(_stop(), timeout_s=10), sess


@live
def test_trace_of_a_pass_that_works(cloudflare, browser_on, tmp_path):  # noqa: F811
    result, trace, _ = _trace(cloudflare, browser_on, tmp_path, honour=True, rotate=False)
    assert result.startswith("# Listings"), result
    analysis = trace["analysis"]
    assert analysis["clearance_issued"] and analysis["challenged"]
    assert [f["code"] for f in analysis["findings"]] == ["passed"]
    assert analysis["exit_addresses"] == ["203.0.113.7"]
    # Chromium's own account of the pass, and never its value.
    considered = [e for e in trace["events"] if e["kind"] == "cookies_considered"]
    assert any(
        c["name"] == "cf_clearance" and c["sent"] for e in considered for c in e["cookies"]
    ), considered
    assert "SECRET" not in repr(trace)
    assert trace["fingerprint"]["userAgent"]


@live
def test_trace_of_a_refused_pass_behind_a_rotating_exit(
    cloudflare,
    browser_on,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from olano.config.system_settings import set_browser_settings
    from olano.tools import browser as browser_tools

    monkeypatch.setattr(browser_tools, "_has_screen", lambda sess: True)
    set_browser_settings({"human_check": "wait", "human_check_wait": 1})
    result, trace, sess = _trace(cloudflare, browser_on, tmp_path, honour=False, rotate=True)
    assert "nobody completed" in result, result
    codes = [f["code"] for f in trace["analysis"]["findings"]]
    assert codes[0] == "exit_changes" and "clearance_rejected" in codes, codes
    kinds = [e["kind"] for e in trace["events"]]
    assert "check_started" in kinds and "check_ended" in kinds
    # The check nobody passed is replaced by a page that says so, not left to
    # turn into Chromium's "blocked" page at its next refresh.
    assert sess.page.url.startswith("data:text/html"), sess.page.url
    assert "Stopped at" in browser_on_title(sess)


def browser_on_title(sess) -> str:
    from olano.browser import loop as browser_loop

    return browser_loop.run_sync(sess.page.title(), timeout_s=10)


# ── the API ───────────────────────────────────────────────────────────────────


def test_api_reads_the_last_trace_and_needs_an_open_browser_to_start(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.config.agent_config import AgentConfig
    from olano.config.loader import save_agent_config
    from olano.runtime.api import create_api

    save_agent_config(AgentConfig(name="scout"))
    client = TestClient(create_api(MagicMock()))
    url = "/api/agents/scout/browser/debug-trace"
    assert client.get(url).status_code == 404
    assert client.post(url, json={"action": "start"}).status_code == 409
    assert client.post(url, json={"action": "go"}).status_code == 400
    monkeypatch.setitem(debug_trace._last, "scout", {"agent": "scout", "events": []})
    assert client.get(url).json() == {"agent": "scout", "events": []}


def test_cloudflares_unresolvable_probe_host_is_not_a_finding():
    """Every browser fails ``brunhild.challenges.cloudflare.com`` (no public address)."""
    probe = {
        "t": 1,
        "kind": "request_failed",
        "url": "https://brunhild.challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/i/…",
        "error": "net::ERR_NAME_NOT_RESOLVED",
    }
    real = {
        **probe,
        "url": "https://challenges.cloudflare.com/cdn-cgi/x",
        "error": "net::ERR_FAILED",
    }
    assert _codes({"events": [probe]}) == []
    assert _codes({"events": [probe, real]}) == ["challenge_requests_failed"]
