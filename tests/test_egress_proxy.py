"""The sandbox egress proxy and its firewall, without needing docker.

The container-boundary facts live in
``tests/integration/test_sandbox_egress_live.py``; everything here is the pure
half — the decision, the request parsing, and the rule the enforcement layer
would install — so CI covers the logic on a runner with no daemon.
"""

from __future__ import annotations

import http.server
import subprocess
import threading

import pytest

from olano.security import egress_firewall
from olano.security.egress import EgressPolicy
from olano.security.egress_proxy import (
    _split_hostport,
    _target_from_request,
    check,
    ensure_proxy,
    proxy_env_for_agent,
    stop_all,
)


@pytest.fixture(autouse=True)
def _no_listeners_left_behind():
    yield
    stop_all()


# ── the decision ─────────────────────────────────────────────────────────────


def test_allow_deny_and_subdomains():
    policy = EgressPolicy(allow=("example.com",), deny=("bad.example.com",))
    assert check("example.com", policy) is None
    assert check("files.example.com", policy) is None
    # Deny is checked first, so a denied subdomain of an allowed domain stays denied.
    assert check("bad.example.com", policy) is not None
    assert check("elsewhere.test", policy) is not None


def test_open_policy_allows_everything():
    assert check("anything.test", EgressPolicy()) is None


def test_infra_floor_outranks_a_deny():
    """A sandbox cannot be cut off from the control plane any more than the
    runtime can — the same floor, so the two layers cannot disagree."""
    policy = EgressPolicy(allow=("example.com",), deny=("olano.ai",))
    assert check("cloud.olano.ai", policy) is None


def test_a_missing_host_is_refused():
    assert check("", EgressPolicy(allow=("x.com",))) is not None


# ── request parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target, expected",
    [
        ("example.com:443", ("example.com", 443)),
        ("example.com", ("example.com", 443)),
        ("[::1]:8080", ("::1", 8080)),
        ("[::1]", ("::1", 443)),
    ],
)
def test_split_hostport(target, expected):
    assert _split_hostport(target, 443) == expected


def test_connect_and_absolute_form_both_parse():
    assert _target_from_request("CONNECT example.com:443 HTTP/1.1") == ("example.com", 443, True)
    assert _target_from_request("GET http://example.com/a/b HTTP/1.1") == ("example.com", 80, False)
    assert _target_from_request("GET https://example.com/x HTTP/1.1") == ("example.com", 443, False)
    # A plain origin-form request has no host to check and is not proxyable.
    assert _target_from_request("GET /path HTTP/1.1") is None
    assert _target_from_request("garbage") is None


# ── the listener ─────────────────────────────────────────────────────────────


@pytest.fixture
def upstream():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def _curl(proxy_url: str, url: str) -> str:
    """curl through *proxy_url*, with the ambient no-proxy list neutralised.

    `--noproxy ""` is load-bearing and not tidiness: a developer box (and this
    repo's own sandbox) commonly exports NO_PROXY=localhost,127.0.0.1, and curl
    honours it *over* `-x`. Without this the client connects straight to the
    upstream, the request never touches the listener under test, and the
    assertion passes for entirely the wrong reason.
    """
    proc = subprocess.run(
        ["curl", "-sS", "-x", proxy_url, "--noproxy", "", "-m", "10", url],
        capture_output=True,
        text=True,
    )
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


@pytest.mark.skipif(
    not subprocess.run(["which", "curl"], capture_output=True).returncode == 0,
    reason="curl not available",
)
def test_proxy_tunnels_an_allowed_host_and_refuses_the_rest(upstream):
    policy = EgressPolicy(allow=("localhost",), deny=("blocked.test",))
    handle = ensure_proxy("unit", "127.0.0.1", lambda: policy)

    assert _curl(handle.url, f"http://localhost:{upstream}/") == "ok"
    refused = _curl(handle.url, "http://blocked.test/")
    assert "outbound access policy" in refused
    assert "deny list" in refused
    assert "not on the allow list" in _curl(handle.url, "http://unlisted.test/")


def test_a_policy_that_cannot_be_resolved_refuses(upstream):
    """Fail CLOSED here, unlike the runtime guard. The proxy only exists
    because a restriction was asked for, so an unreadable policy must not
    quietly turn it into an open relay."""

    def broken():
        raise RuntimeError("no policy for you")

    handle = ensure_proxy("broken", "127.0.0.1", broken)
    assert "could not be read" in _curl(handle.url, f"http://localhost:{upstream}/")


def test_ensure_proxy_is_idempotent_per_agent():
    policy = EgressPolicy(allow=("x.com",))
    first = ensure_proxy("same", "127.0.0.1", lambda: policy)
    second = ensure_proxy("same", "127.0.0.1", lambda: policy)
    assert (first.host, first.port) == (second.host, second.port)


def test_two_agents_get_two_ports():
    """The port is the identity — that is how the proxy knows whose policy
    applies without a header, a token or a lookup."""
    policy = EgressPolicy(allow=("x.com",))
    a = ensure_proxy("agent-a", "127.0.0.1", lambda: policy)
    b = ensure_proxy("agent-b", "127.0.0.1", lambda: policy)
    assert a.port != b.port


# ── the sandbox seam ─────────────────────────────────────────────────────────


def test_an_open_agent_gets_no_proxy_env():
    """The important default: an untouched box gets no proxy, no environment
    and no extra hop in front of every shell command."""
    assert proxy_env_for_agent("open", "bridge", lambda: EgressPolicy()) == {}


def test_a_resolver_that_raises_yields_no_env():
    def broken():
        raise RuntimeError("boom")

    assert proxy_env_for_agent("broken", "bridge", broken) == {}


def test_env_carries_both_cases_of_every_name(monkeypatch):
    """curl and python read the lowercase names, most Go and Node tooling reads
    the uppercase ones — setting only one silently misses half the toolchain."""
    monkeypatch.setattr("olano.security.egress_proxy.bridge_gateway", lambda _net: "127.0.0.1")
    env = proxy_env_for_agent("cased", "bridge", lambda: EgressPolicy(allow=("x.com",)))
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        assert name in env
    assert "127.0.0.11" in env["NO_PROXY"]  # docker's embedded resolver


def test_no_env_when_the_gateway_cannot_be_resolved(monkeypatch, caplog):
    """Never point a sandbox at an address nothing is listening on — and say so,
    because the shell is then unrestricted and that must not be silent."""
    monkeypatch.setattr("olano.security.egress_proxy.bridge_gateway", lambda _net: None)
    import logging

    with caplog.at_level(logging.WARNING):
        assert proxy_env_for_agent("nogw", "bridge", lambda: EgressPolicy(allow=("x.com",))) == {}
    assert "NOT restricted" in caplog.text


# ── the firewall's pure half ─────────────────────────────────────────────────


def test_rule_order_puts_the_tcp_reject_above_the_drop():
    """Both are inserted with -I, so the last inserted matches first. Reversed,
    every TCP connection would hit the drop and hang — which is the exact
    behaviour the reject exists to avoid."""
    specs = egress_firewall._rule_specs("172.17.0.2", "olano-egress:x")
    assert specs[0][-1] == "DROP"
    assert "tcp-reset" in specs[1]


def test_the_tag_is_bounded_and_sanitised():
    """netfilter rejects a comment over 256 bytes outright, so an agent with a
    long or odd name must not produce a rule that cannot be installed."""
    tag = egress_firewall._tag_for("../weird name/" + "x" * 500)
    assert len(tag) < 200
    assert " " not in tag and "/" not in tag
    assert tag.startswith("olano-egress:")


def test_an_empty_ip_is_never_applied():
    assert egress_firewall.apply_for("someone", "") is False
