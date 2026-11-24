"""The sandbox half of the egress lists: a policy-checking forward proxy.

:mod:`olano.security.egress_guard` governs what the *runtime* reaches, by
patching the HTTP libraries it calls. Nothing it does can reach the agent's
**shell**: a ``curl``, ``pip install`` or ``git clone`` inside ``execute`` runs
in a container, in its own process, with its own sockets. That lane needs an
enforcement point outside the process, and a domain list cannot be an iptables
rule — ``api.github.com`` is a rotating CDN, so filtering by resolved IP either
blocks a legitimate host tomorrow or misses it today.

So: a forward proxy the sandbox is pointed at, filtering on the host *name* the
client asks for.

* ``CONNECT host:port`` — checked against the policy, then a blind TCP tunnel.
  **No TLS interception**: the host name is in the CONNECT line in clear, which
  is all the decision needs, so there is no CA to distribute, nothing to pin,
  and the proxy never sees a byte of plaintext.
* Absolute-form plain HTTP (``GET http://host/path``) — checked the same way
  and forwarded, because package managers still use it for mirrors.

**One listener per agent, and the port is the identity.** A container is handed
``HTTPS_PROXY=http://<gateway>:<port>``; that port belongs to one agent, so the
proxy needs no header, no token and no lookup to know whose policy applies. It
binds to the **docker bridge gateway address only** — never ``0.0.0.0``, which
on a cloud box can be a public interface.

**It starts only for an agent that has a non-empty policy.** The default is two
empty lists, and a box in that state gets no proxy, no env and no change of any
kind: routing every sandbox command through a new hop by default would put a
process in the path of every agent's shell to enforce a policy that permits
everything. :func:`proxy_env_for_agent` returns ``{}`` there, and the caller
injects nothing.

Failure posture is deliberate and is the opposite of the runtime guard's. There,
an unreadable config means "no policy", because the alternative is a typo taking
a box offline. Here, the proxy is only ever running *because* an operator asked
for a restriction, so a policy that cannot be resolved refuses the connection
rather than tunnelling it: by the time we are in this code the question is not
"should anything be enforced" but "what", and a proxy that fails open is a
proxy that silently is not one.
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from olano.security.egress import host_matches
from olano.security.egress_guard import INFRA_HOSTS

if TYPE_CHECKING:
    from olano.security.egress import EgressPolicy

logger = logging.getLogger(__name__)

#: How long to wait for a client's request line before giving up. A sandbox
#: client that connects and says nothing must not hold a thread forever.
REQUEST_TIMEOUT_S = 30.0
#: Relay buffer. Large enough that a package download is not syscall-bound.
_CHUNK = 65536
_MAX_REQUEST_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProxyHandle:
    """A running per-agent listener."""

    agent: str
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _refusal(host: str, reason: str) -> bytes:
    """A 403 an agent can actually read.

    The body matters: `curl` prints it, so the refusal reaches the model as a
    sentence naming the host and the fix rather than a bare status code.
    """
    body = (
        f"Blocked by this deployment's outbound access policy: {reason}\n"
        f"Host: {host}\n"
        "Add it under Config -> Outbound access, or to this agent's egress_allow.\n"
    ).encode()
    return (
        b"HTTP/1.1 403 Forbidden\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Proxy-Connection: close\r\n"
        b"Connection: close\r\n\r\n" + body
    )


def check(host: str, policy: EgressPolicy) -> str | None:
    """Return a refusal reason for *host* under *policy*, or ``None`` to allow.

    Same order as every other surface — deny first, then allow — and the same
    infrastructure floor, so a sandbox cannot be cut off from the control plane
    any more than the runtime can.
    """
    lowered = (host or "").strip().lower().rstrip(".")
    if not lowered:
        return "no host in the request"
    if host_matches(lowered, INFRA_HOSTS):
        return None
    if policy.deny and host_matches(lowered, policy.deny):
        return "the host is on the deny list"
    if policy.allow and not host_matches(lowered, policy.allow):
        return "the host is not on the allow list"
    return None


def _split_hostport(target: str, default_port: int) -> tuple[str, int]:
    """``host:port`` → ``(host, port)``. IPv6 literals keep their brackets."""
    text = target.strip()
    if text.startswith("["):  # [::1]:443
        close = text.find("]")
        if close != -1:
            host = text[1:close]
            rest = text[close + 1 :]
            port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
            return host, port
    if ":" in text:
        head, _, tail = text.rpartition(":")
        if tail.isdigit():
            return head, int(tail)
    return text, default_port


def _target_from_request(line: str) -> tuple[str, int, bool] | None:
    """``(host, port, is_connect)`` from a request line, or ``None``.

    Handles the two forms a proxy sees: ``CONNECT host:port`` and an
    absolute-form ``GET http://host/path``.
    """
    parts = line.split()
    if len(parts) < 2:
        return None
    method, target = parts[0].upper(), parts[1]
    if method == "CONNECT":
        host, port = _split_hostport(target, 443)
        return host, port, True
    if "://" in target:
        from urllib.parse import urlsplit

        parsed = urlsplit(target)
        if parsed.hostname:
            return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), False
    return None


class _ProxyServer(threading.Thread):
    """One agent's listener. A thread per connection — a sandbox makes few."""

    daemon = True

    def __init__(self, agent: str, bind_host: str, resolve_policy) -> None:
        super().__init__(name=f"egress-proxy-{agent}")
        self.agent = agent
        self._resolve_policy = resolve_policy
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bind to the bridge gateway only. Binding 0.0.0.0 would expose an open
        # forward proxy on whatever else the host is attached to, which on a
        # cloud box is the public internet.
        self._sock.bind((bind_host, 0))
        self._sock.listen(64)
        self.host, self.port = self._sock.getsockname()
        self._stopping = threading.Event()

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                client, _addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(
                target=self._serve, args=(client,), daemon=True, name=f"egress-conn-{self.agent}"
            ).start()

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._sock.close()
        except OSError:
            pass

    # ── one connection ───────────────────────────────────────────────────────

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(REQUEST_TIMEOUT_S)
            head = self._read_head(client)
            if head is None:
                return
            line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            target = _target_from_request(line)
            if target is None:
                client.sendall(_refusal("", "the proxy could not read the request"))
                return
            host, port, is_connect = target

            try:
                policy = self._resolve_policy()
            except Exception:
                # Fail closed: this proxy exists only because a restriction was
                # asked for, so an unresolvable policy refuses rather than
                # silently becoming a plain open relay.
                logger.exception("egress proxy could not resolve policy for %r", self.agent)
                client.sendall(_refusal(host, "the outbound access policy could not be read"))
                return

            reason = check(host, policy)
            if reason:
                logger.info("egress proxy refused %s for agent %r: %s", host, self.agent, reason)
                client.sendall(_refusal(host, reason))
                return

            upstream = socket.create_connection((host, port), timeout=REQUEST_TIMEOUT_S)
            if is_connect:
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                upstream.sendall(head)  # plain HTTP: replay what we consumed
            client.settimeout(None)
            upstream.settimeout(None)
            self._relay(client, upstream)
        except Exception as exc:  # a bad client must never take the listener down
            logger.debug("egress proxy connection for %r ended: %s", self.agent, exc)
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    @staticmethod
    def _read_head(client: socket.socket) -> bytes | None:
        """Read up to the end of the request headers, bounded."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > _MAX_REQUEST_BYTES:
                return None
            chunk = client.recv(_CHUNK)
            if not chunk:
                return None
            buf += chunk
        return buf

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket) -> None:
        """Pump both directions until either side closes."""

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(_CHUNK)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(a, b), daemon=True)
        t.start()
        pump(b, a)
        t.join(timeout=5)


# ── registry ────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_servers: dict[str, _ProxyServer] = {}


def ensure_proxy(agent: str, bind_host: str, resolve_policy) -> ProxyHandle:
    """Return this agent's listener, starting one if needed. Idempotent."""
    with _lock:
        server = _servers.get(agent)
        if server is None or not server.is_alive():
            server = _ProxyServer(agent, bind_host, resolve_policy)
            server.start()
            _servers[agent] = server
            logger.info(
                "egress proxy for agent %r listening on %s:%s", agent, server.host, server.port
            )
        return ProxyHandle(agent=agent, host=server.host, port=server.port)


def stop_proxy(agent: str) -> None:
    """Stop and forget this agent's listener. Safe to call when none exists."""
    with _lock:
        server = _servers.pop(agent, None)
    if server is not None:
        server.stop()


def stop_all() -> None:
    """Stop every listener — runtime shutdown, and the test teardown."""
    with _lock:
        servers = list(_servers.values())
        _servers.clear()
    for server in servers:
        server.stop()


def active_agents() -> list[str]:
    """Names with a live listener. Diagnostics."""
    with _lock:
        return sorted(name for name, srv in _servers.items() if srv.is_alive())


# ── the sandbox seam ────────────────────────────────────────────────────────

#: Hosts a sandbox must always reach without going through the proxy: its own
#: loopback, and the docker-embedded resolver. Handed to the client as NO_PROXY.
_NO_PROXY = "localhost,127.0.0.1,::1,127.0.0.11"


def bridge_gateway(network: str) -> str | None:
    """The gateway address of a docker network — where the proxy binds.

    This is the address the container already routes through, so it is
    reachable from inside without any extra plumbing, and it is not publicly
    routable. Returns ``None`` when docker cannot answer, and the caller then
    injects no proxy rather than pointing a sandbox at an address that is not
    listening.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "docker",
                "network",
                "inspect",
                network,
                "-f",
                "{{range .IPAM.Config}}{{.Gateway}}{{end}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:  # pragma: no cover - docker absent
        logger.debug("could not inspect docker network %r: %s", network, exc)
        return None
    gateway = (proc.stdout or "").strip()
    return gateway or None


def proxy_env_for_agent(agent: str, network: str, policy_resolver) -> dict[str, str]:
    """Proxy environment for one agent's sandbox, or ``{}`` when ungoverned.

    ``{}`` is the common answer and the important one: an agent whose lists are
    both empty gets no proxy, no environment and no extra hop, so a default box
    behaves exactly as it did before this existed.

    Called per ``docker exec`` rather than baked into ``docker run``, which is
    what keeps a policy change out of the container signature: the env rides
    the command, so the next shell command picks up a new policy with no
    container recreate.
    """
    try:
        policy = policy_resolver()
    except Exception:
        logger.exception("could not resolve the egress policy for agent %r", agent)
        return {}
    if policy.is_open:
        return {}
    gateway = bridge_gateway(network)
    if not gateway:
        logger.warning(
            "agent %r has an egress policy but the gateway of docker network %r could not "
            "be resolved — the sandbox shell is NOT restricted for this command",
            agent,
            network,
        )
        return {}
    handle = ensure_proxy(agent, gateway, policy_resolver)
    url = handle.url
    # Both cases of each name: Python, curl and wget read the lowercase ones,
    # most Go and Node tooling reads the uppercase ones, and git reads both.
    return {
        "http_proxy": url,
        "https_proxy": url,
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "no_proxy": _NO_PROXY,
        "NO_PROXY": _NO_PROXY,
    }
