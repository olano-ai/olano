"""Every connection an agent's local browser opens, checked by the egress guard.

The guard (:mod:`olano.browser.guard`) checks each HTTP request in a route
handler. A WebSocket never reaches one: Chromium's own request interception
does not pause a WebSocket handshake, and Playwright's ``route_web_socket``
puts a binding on every page, which is what made Cloudflare challenge the
agent's browser on the next page it loaded (measured on propertyguru.com.sg,
browser/plain_check.py). So a local context's traffic goes through a proxy
of ours instead (:mod:`olano.browser._egress_proxy_core`), and the proxy asks
the guard about each connection before making it, WebSockets included:

- the cloud metadata addresses, never;
- the agent's allow/deny lists (:class:`olano.browser.guard.GuardPolicy`,
  refreshed on every tool call, read at connection time);
- the private-network block: the name is resolved here and the proxy connects
  to the very addresses that were checked (:func:`olano.security.ssrf.resolve_public`).

Where the proxy runs is where the browser runs: in the runtime for the shared
headless Chromium (:func:`start_local`), inside the agent's ``olano-browser``
container for the live browser (:func:`start_in_container`, the decisions
crossing the ``docker exec`` pipe), so the site sees the address it saw
before. The agent's own ``BROWSER_PROXY_URL`` becomes the proxy's upstream.
Loopback goes through it too (:data:`BYPASS`): Chromium sends ``localhost``
straight out otherwise.

The remote provider's browser is not on this machine and keeps
``route_web_socket``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from olano.browser import _egress_proxy_core as core
from olano.browser.guard import GuardState, metadata_refusal

logger = logging.getLogger(__name__)

# Chromium's proxy bypass list: "<-loopback>" takes away the implicit bypass
# for localhost, 127.0.0.1 and [::1], so those reach the proxy too.
BYPASS = "<-loopback>"
START_TIMEOUT_S = 30.0
# A page opens dozens of connections to a handful of hosts: each name is
# resolved and checked once a minute, as the guard does for HTTP requests.
_DNS_TTL_S = 60.0
_DNS_CACHE_MAX = 512
_resolved: dict[tuple[str, int], tuple[float, str | None, list[str]]] = {}


def _resolve_public_cached(host: str, port: int) -> tuple[str | None, list[str]]:
    from olano.security.ssrf import resolve_public

    key = (host.lower(), port)
    now = time.monotonic()
    hit = _resolved.get(key)
    if hit is not None and now - hit[0] < _DNS_TTL_S:
        return hit[1], list(hit[2])
    reason, addresses = resolve_public(host, port)
    if len(_resolved) >= _DNS_CACHE_MAX:
        _resolved.clear()
    _resolved[key] = (now, reason, list(addresses))
    return reason, addresses


def clear_dns_cache() -> None:
    _resolved.clear()


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


async def check(state: GuardState, host: str, port: int) -> dict[str, Any]:
    """The guard's answer for a connection to *host*:*port* (see the module)."""
    policy = state.policy
    reason = metadata_refusal(host)
    if not reason:
        from olano.security.egress_guard import check_host_against

        reason = check_host_against(host, (policy.allow, policy.deny))
    addresses: list[str] = []
    if not reason and policy.block_private:
        reason, addresses = await asyncio.get_running_loop().run_in_executor(
            None, _resolve_public_cached, host, port
        )
        if not reason and not addresses:
            # Not a refusal: the name does not exist (or not from here).
            return {"ok": False, "status": "502 Bad Gateway", "reason": f"'{host}' not found."}
    if reason:
        state.note(f"//{_url_host(host)}:{port}", reason)
        return {"ok": False, "reason": reason}
    return {"ok": True, "connect": addresses}


def upstream_of(proxy: dict[str, str] | None) -> dict[str, Any] | None:
    """The core's upstream for a Playwright ``proxy`` option
    (:func:`olano.browser.launch.parse_proxy`), or ``None``."""
    if not proxy or not proxy.get("server"):
        return None
    parts = urlsplit(proxy["server"])
    scheme = parts.scheme.lower()
    return {
        "scheme": scheme,
        "host": parts.hostname or "",
        "port": parts.port or (1080 if scheme.startswith("socks") else 8080),
        "username": proxy.get("username", ""),
        "password": proxy.get("password", ""),
    }


class EgressProxy:
    """A running proxy for one browser context."""

    port: int = 0

    def playwright_proxy(self) -> dict[str, str]:
        """The context's ``proxy`` option: this proxy, loopback included."""
        return {"server": f"http://127.0.0.1:{self.port}", "bypass": BYPASS}

    def alive(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class LocalProxy(EgressProxy):
    def __init__(self, server: Any) -> None:
        self.server = server
        self.port = int(server.sockets[0].getsockname()[1])

    def alive(self) -> bool:
        return bool(self.server.is_serving())

    async def close(self) -> None:
        self.server.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), 5)
        except (TimeoutError, Exception):
            pass


async def start_local(state: GuardState, upstream: dict[str, str] | None = None) -> LocalProxy:
    """A proxy in this process, for a browser on this machine."""

    async def decide(host: str, port: int) -> dict[str, Any]:
        return await check(state, host, port)

    server = await core.serve(decide, upstream=upstream_of(upstream))
    return LocalProxy(server)


def core_source() -> str:
    return Path(core.__file__).read_text(encoding="utf-8")


class ContainerProxy(EgressProxy):
    """The proxy running inside a browser container, over ``docker exec``."""

    def __init__(self, proc: asyncio.subprocess.Process, state: GuardState) -> None:
        self.proc = proc
        self.state = state
        self.task: asyncio.Task[None] | None = None

    def alive(self) -> bool:
        """The ``docker exec`` is still running: when it is not (the daemon
        dropped it), every connection of the browser fails, so the session is
        reopened rather than kept."""
        return self.proc.returncode is None and (self.task is None or not self.task.done())

    async def _answer(self, ask: dict[str, Any]) -> None:
        try:
            verdict = await check(self.state, str(ask.get("host") or ""), int(ask.get("port") or 0))
        except Exception as exc:  # fail closed
            verdict = {"ok": False, "reason": f"Connection check failed: {exc}"}
        self._send({"id": ask.get("id"), **verdict})

    def _send(self, payload: dict[str, Any]) -> None:
        stdin = self.proc.stdin
        if stdin is None or stdin.is_closing():
            return
        stdin.write((json.dumps(payload) + "\n").encode())

    async def serve(self) -> None:
        assert self.proc.stdout is not None
        pending: set[asyncio.Task[None]] = set()
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                ask = json.loads(line)
            except ValueError:
                continue
            if isinstance(ask, dict) and "id" in ask:
                task = asyncio.get_running_loop().create_task(self._answer(ask))
                pending.add(task)
                task.add_done_callback(pending.discard)
        logger.info("browser: the egress proxy in its container stopped")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), 5)
        except Exception:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


def container_argv(container: str) -> list[str]:
    return ["docker", "exec", "-i", container, "python3", "-c", core_source()]


async def start_in_container(
    container: str, state: GuardState, upstream: dict[str, str] | None = None
) -> ContainerProxy:
    """The proxy inside *container*, next to the browser it serves. The
    upstream's credentials cross on stdin, never on a command line."""
    proc = await asyncio.create_subprocess_exec(
        *container_argv(container),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proxy = ContainerProxy(proc, state)
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps({"upstream": upstream_of(upstream)}) + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), START_TIMEOUT_S)
        proxy.port = int(json.loads(line)["port"])
    except Exception as exc:
        await proxy.close()
        raise RuntimeError(f"The browser's egress proxy did not start: {exc}") from exc
    proxy.task = asyncio.get_running_loop().create_task(proxy.serve())
    return proxy
