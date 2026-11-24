"""The sandbox half of the egress lists, against a real container.

The unit suite can prove the argv and the rule strings; it cannot prove the two
facts this feature actually rests on, both of which are the kernel's opinion and
not ours:

1. a ``DOCKER-USER`` rule matching a container's source address cuts it off from
   the world **while leaving the lane to the bridge gateway open** — that
   asymmetry is what lets the proxy stay reachable from inside a confined
   container, and it holds because container → gateway is ``INPUT`` on the host
   rather than forwarded;
2. ``REJECT --reject-with tcp-reset`` refuses a blocked connection *instantly*,
   where a ``DROP`` costs the client its full timeout. Measured here rather
   than asserted, because "the agent waits six seconds to learn it may not
   reach a host, on every attempt, inside one turn" is the difference between a
   usable policy and one an operator turns back off.

Everything runs against a tiny public base image and a local HTTP server bound
on the bridge gateway, so the suite needs no internet and no private registry.
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading
import time

import pytest

from olano.security import egress_firewall
from olano.security.egress import EgressPolicy
from olano.security.egress_proxy import bridge_gateway, ensure_proxy, stop_proxy

#: Any tiny image with a shell and busybox wget. Overridable because Docker Hub
#: is unreachable from some networks; `mirror.gcr.io/library/alpine:3.20` and
#: `public.ecr.aws/docker/library/alpine:3.20` both work and can be tagged local.
IMAGE = os.environ.get("LIONCLAW_EGRESS_TEST_IMAGE", "alpine:3.20")
AGENT = "egress-live-test"


def _docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
            return False, "no Docker daemon"
        probe = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker unavailable: {exc}"
    if probe.returncode != 0:
        return False, f"test image {IMAGE!r} not present locally"
    return True, ""


_READY, _WHY = _docker_ready()
pytestmark = pytest.mark.skipif(not _READY, reason=f"live sandbox egress unavailable — {_WHY}")


@pytest.fixture(scope="module")
def gateway() -> str:
    gw = bridge_gateway("bridge")
    if not gw:
        pytest.skip("could not resolve the docker bridge gateway")
    return gw


@pytest.fixture(scope="module")
def upstream(gateway: str):
    """A server on the bridge gateway standing in for 'somewhere on the internet'."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # silence
            return

    server = http.server.HTTPServer((gateway, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def container():
    cid = subprocess.run(
        ["docker", "run", "-d", "--rm", IMAGE, "sleep", "300"],
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()
    if not cid:
        pytest.skip("could not start a container")
    ip = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            cid,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    yield cid, ip
    egress_firewall.clear_for(AGENT, ip)
    subprocess.run(["docker", "kill", cid], capture_output=True, timeout=60)


def _wget(cid: str, url: str, timeout_s: int = 8, env: dict[str, str] | None = None) -> str:
    argv = ["docker", "exec"]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += [cid, "wget", "-q", "-O-", "-T", str(timeout_s), url]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s + 30)
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


# ── the proxy ────────────────────────────────────────────────────────────────


def test_proxy_allows_and_refuses_from_inside_a_container(container, gateway, upstream):
    """The shell lane end to end: an allowed host tunnels, a blocked one comes
    back as a message the agent can read rather than a silent failure."""
    cid, _ip = container
    # The gateway's own address is the allowed "host": the proxy resolves the
    # target itself, so using an address keeps the test free of any DNS setup
    # while exercising exactly the same matcher.
    policy = EgressPolicy(allow=(gateway,), deny=("blocked.test",))
    handle = ensure_proxy(AGENT, gateway, lambda: policy)
    try:
        env = {"http_proxy": handle.url, "https_proxy": handle.url}
        allowed = _wget(cid, f"http://{gateway}:{upstream}/", env=env)
        # Both refusals happen before the proxy resolves anything, so these
        # names never need to exist.
        blocked = _wget(cid, f"http://blocked.test:{upstream}/", env=env)
        unlisted = _wget(cid, f"http://unlisted.test:{upstream}/", env=env)
    finally:
        stop_proxy(AGENT)

    assert allowed == "ok"
    assert "403" in blocked
    assert "403" in unlisted


# ── the firewall ─────────────────────────────────────────────────────────────


def test_rules_block_the_world_but_not_the_proxy_lane(container, gateway, upstream):
    """The asymmetry the whole design rests on. Container → gateway is INPUT on
    the host, not forwarded, so DOCKER-USER never sees it; container → anywhere
    else is forwarded and does."""
    cid, ip = container
    if not egress_firewall.available():
        pytest.skip("iptables/DOCKER-USER not usable here")

    assert egress_firewall.apply_for(AGENT, ip) is True
    assert len(egress_firewall.rules_for(AGENT)) == 2

    # The proxy lane (container → bridge gateway) is untouched…
    assert _wget(cid, f"http://{gateway}:{upstream}/") == "ok"
    # …while a forwarded destination is refused.
    assert "ok" not in _wget(cid, "http://1.1.1.1/", timeout_s=5)


def test_apply_is_idempotent(container):
    cid, ip = container
    if not egress_firewall.available():
        pytest.skip("iptables/DOCKER-USER not usable here")
    egress_firewall.apply_for(AGENT, ip)
    egress_firewall.apply_for(AGENT, ip)
    assert len(egress_firewall.rules_for(AGENT)) == 2


def test_blocked_connection_is_refused_not_hung(container):
    """`tcp-reset` over `DROP`: a blocked call must fail in well under a second.

    A drop makes the client burn its whole timeout, and an agent retries — so
    the difference compounds inside a single turn.
    """
    cid, ip = container
    if not egress_firewall.available():
        pytest.skip("iptables/DOCKER-USER not usable here")
    egress_firewall.apply_for(AGENT, ip)

    started = time.monotonic()
    _wget(cid, "http://1.1.1.1/", timeout_s=8)
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, (
        f"a blocked connection took {elapsed:.1f}s — is it DROP rather than REJECT?"
    )


def test_clear_removes_only_this_agents_rules(container):
    cid, ip = container
    if not egress_firewall.available():
        pytest.skip("iptables/DOCKER-USER not usable here")
    egress_firewall.apply_for(AGENT, ip)
    egress_firewall.apply_for("someone-else", ip)
    try:
        assert egress_firewall.clear_for(AGENT, ip) == 2
        assert egress_firewall.rules_for(AGENT) == []
        assert len(egress_firewall.rules_for("someone-else")) == 2
    finally:
        egress_firewall.clear_for("someone-else", ip)


def test_sweep_drops_rules_whose_container_is_gone(container):
    """A rule keyed on an address docker reuses must not outlive its container."""
    cid, ip = container
    if not egress_firewall.available():
        pytest.skip("iptables/DOCKER-USER not usable here")
    egress_firewall.apply_for(AGENT, ip)

    # The address is not in the live set, so the sweep must forget it.
    assert egress_firewall.sweep_stale(live_ips=set()) >= 2
    assert egress_firewall.rules_for(AGENT) == []


def test_a_live_container_survives_the_sweep(container):
    cid, ip = container
    if not egress_firewall.available():
        pytest.skip("iptables/DOCKER-USER not usable here")
    egress_firewall.apply_for(AGENT, ip)
    egress_firewall.sweep_stale(live_ips={ip})
    assert len(egress_firewall.rules_for(AGENT)) == 2
