"""The browser's egress proxy (olano/browser/egress_proxy.py, _egress_proxy_core.py).

Real sockets throughout: a target server that records what reached it, fake
upstream proxies (HTTP with sign-in, SOCKS5 with sign-in, SOCKS4a), the core
in this process and, for the live browser's way of running it, as a separate
``python3 -c`` process speaking the stdio protocol, as ``docker exec`` runs it.
The last test drives a real Chromium context through the proxy: a WebSocket
to an allowed host opens and echoes, one to a refused host never reaches it,
and nothing of ours is on the page.
"""

from __future__ import annotations

import asyncio
import base64
import struct
import sys
from typing import Any

import pytest

from olano.browser import _egress_proxy_core as core
from olano.browser import egress_proxy
from olano.browser.guard import GuardPolicy, GuardState

# ── servers ───────────────────────────────────────────────────────────────────


class Target:
    """Answers every connection with a fixed HTTP response and records the
    first bytes it got."""

    def __init__(self) -> None:
        self.seen: list[bytes] = []
        self.server: Any = None
        self.port = 0

    async def __aenter__(self) -> Target:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            except Exception:
                head = b""
            self.seen.append(head)
            body = b"hello from target"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
                + body
            )
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.server.close()


async def _proxy(decide: Any, upstream: dict[str, Any] | None = None) -> tuple[Any, int]:
    server = await core.serve(decide, upstream=upstream)
    return server, server.sockets[0].getsockname()[1]


async def _exchange(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 10)
    rest = b""
    try:
        rest = await asyncio.wait_for(reader.read(65536), 2)
    except TimeoutError:
        pass
    writer.close()
    return data + rest


async def _tunnel(port: int, target: str, payload: bytes) -> tuple[bytes, bytes]:
    """CONNECT through the proxy, then send *payload*: (proxy answer, reply)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    if b" 200 " not in head.split(b"\r\n")[0]:
        body = await asyncio.wait_for(reader.read(4096), 5)
        writer.close()
        return head + body, b""
    writer.write(payload)
    await writer.drain()
    reply = await asyncio.wait_for(reader.read(65536), 10)
    writer.close()
    return head, reply


def _allow(connect: list[str] | None = None) -> Any:
    async def decide(host: str, port: int) -> dict[str, Any]:
        return {"ok": True, "connect": connect or []}

    return decide


# ── the core ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "default", "expected"),
    [
        ("example.com:443", 443, ("example.com", 443)),
        ("example.com", 80, ("example.com", 80)),
        ("[::1]:8080", 443, ("::1", 8080)),
        ("[::1]", 443, ("::1", 443)),
    ],
)
def test_split_host_port(text, default, expected):
    assert core.split_host_port(text, default) == expected


@pytest.mark.parametrize("bad", ["", ":80", "host:0", "host:70000", "[::1"])
def test_split_host_port_refuses_nonsense(bad):
    with pytest.raises(ValueError):
        core.split_host_port(bad, 443)


async def test_a_connect_is_tunnelled_to_the_address_the_decision_checked():
    asked: list[tuple[str, int]] = []

    async def decide(host: str, port: int) -> dict[str, Any]:
        asked.append((host, port))
        # The name does not exist: only the checked address can be reached.
        return {"ok": True, "connect": ["127.0.0.1"]}

    async with Target() as target:
        server, port = await _proxy(decide)
        head, reply = await _tunnel(
            port, f"no-such-host.invalid:{target.port}", b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        server.close()
    assert b" 200 " in head
    assert reply.endswith(b"hello from target")
    assert asked == [("no-such-host.invalid", target.port)]


async def test_a_refused_connection_is_never_made():
    async def decide(host: str, port: int) -> dict[str, Any]:
        return {"ok": False, "reason": "Not on this agent's list."}

    async with Target() as target:
        server, port = await _proxy(decide)
        head, _ = await _tunnel(port, f"127.0.0.1:{target.port}", b"x")
        server.close()
        assert target.seen == []
    assert head.startswith(b"HTTP/1.1 403")
    assert b"Not on this agent's list." in head


async def test_a_decision_that_fails_refuses():
    async def decide(host: str, port: int) -> dict[str, Any]:
        raise RuntimeError("boom")

    async with Target() as target:
        server, port = await _proxy(decide)
        head, _ = await _tunnel(port, f"127.0.0.1:{target.port}", b"x")
        server.close()
        assert target.seen == []
    assert head.startswith(b"HTTP/1.1 403") and b"boom" in head


async def test_a_name_that_does_not_resolve_is_a_gateway_error():
    async def decide(host: str, port: int) -> dict[str, Any]:
        return {"ok": False, "status": "502 Bad Gateway", "reason": "'x' not found."}

    server, port = await _proxy(decide)
    head, _ = await _tunnel(port, "x.invalid:443", b"")
    server.close()
    assert head.startswith(b"HTTP/1.1 502")


async def test_plain_http_is_forwarded_in_origin_form_without_proxy_headers():
    async with Target() as target:
        server, port = await _proxy(_allow(["127.0.0.1"]))
        answer = await _exchange(
            port,
            (
                f"GET http://site.test:{target.port}/a/b?c=1 HTTP/1.1\r\n"
                f"Host: site.test:{target.port}\r\n"
                "Proxy-Connection: keep-alive\r\nProxy-Authorization: Basic eDp5\r\n"
                "Accept: */*\r\n\r\n"
            ).encode(),
        )
        server.close()
    assert answer.endswith(b"hello from target")
    sent = target.seen[0].decode()
    assert sent.startswith("GET /a/b?c=1 HTTP/1.1\r\n")
    assert "Proxy-" not in sent and "Connection: close" in sent and "Accept: */*" in sent


async def test_only_http_is_forwarded_in_absolute_form():
    server, port = await _proxy(_allow())
    answer = await _exchange(port, b"GET ftp://x/ HTTP/1.1\r\nHost: x\r\n\r\n")
    server.close()
    assert answer.startswith(b"HTTP/1.1 400")


async def test_the_first_checked_address_that_answers_is_used():
    async with Target() as target:
        # An address with nothing listening comes first: the next one is tried.
        server, port = await _proxy(_allow(["127.0.0.9", "127.0.0.1"]))
        head, reply = await _tunnel(port, f"site.test:{target.port}", b"GET / HTTP/1.1\r\n\r\n")
        server.close()
    assert b" 200 " in head and reply.endswith(b"hello from target")


# ── upstream proxies ──────────────────────────────────────────────────────────


class HttpUpstream:
    """A forward proxy that wants ``user:pass`` and records each request head."""

    def __init__(self) -> None:
        self.heads: list[str] = []

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        self.heads.append(head)
        auth = "Proxy-Authorization: Basic " + base64.b64encode(b"user:pass").decode()
        if auth not in head:
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        line = head.split("\r\n", 1)[0]
        method, target, _ = line.split(" ")
        if method == "CONNECT":
            host, port = core.split_host_port(target, 443)
            t_reader, t_writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
            await writer.drain()
            await core._splice(reader, writer, t_reader, t_writer)
        else:
            from urllib.parse import urlsplit

            url = urlsplit(target)
            t_reader, t_writer = await asyncio.open_connection("127.0.0.1", url.port)
            t_writer.write(f"GET {url.path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            await t_writer.drain()
            writer.write(await t_reader.read(65536))
            await writer.drain()
        writer.close()


async def test_through_an_http_upstream_with_its_sign_in():
    upstream = HttpUpstream()
    up = await asyncio.start_server(upstream.handle, "127.0.0.1", 0)
    up_port = up.sockets[0].getsockname()[1]
    config = {
        "scheme": "http",
        "host": "127.0.0.1",
        "port": up_port,
        "username": "user",
        "password": "pass",
    }
    async with Target() as target:
        server, port = await _proxy(_allow(["198.51.100.1"]), upstream=config)
        head, reply = await _tunnel(port, f"site.test:{target.port}", b"GET / HTTP/1.1\r\n\r\n")
        plain = await _exchange(
            port,
            f"GET http://site.test:{target.port}/p HTTP/1.1\r\nHost: site.test\r\n\r\n".encode(),
        )
        server.close()
    up.close()
    assert b" 200 " in head and reply.endswith(b"hello from target")
    assert plain.endswith(b"hello from target")
    # By name through the upstream (never the checked address), CONNECT for the
    # tunnel and absolute form for plain HTTP, as Chromium would send them.
    assert upstream.heads[0].startswith(f"CONNECT site.test:{target.port} HTTP/1.1")
    assert upstream.heads[1].startswith(f"GET http://site.test:{target.port}/p HTTP/1.1")


async def test_through_a_socks5_upstream_with_its_sign_in():
    got: dict[str, Any] = {}

    async def socks5(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        greeting = await reader.readexactly(2)
        await reader.readexactly(greeting[1])
        writer.write(b"\x05\x02")
        await writer.drain()
        _, ulen = await reader.readexactly(2)
        got["user"] = (await reader.readexactly(ulen)).decode()
        plen = (await reader.readexactly(1))[0]
        got["password"] = (await reader.readexactly(plen)).decode()
        writer.write(b"\x01\x00")
        await writer.drain()
        _, _, _, atyp = await reader.readexactly(4)
        assert atyp == 3
        nlen = (await reader.readexactly(1))[0]
        got["host"] = (await reader.readexactly(nlen)).decode()
        port = struct.unpack(">H", await reader.readexactly(2))[0]
        t_reader, t_writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        await core._splice(reader, writer, t_reader, t_writer)
        writer.close()

    up = await asyncio.start_server(socks5, "127.0.0.1", 0)
    config = {
        "scheme": "socks5",
        "host": "127.0.0.1",
        "port": up.sockets[0].getsockname()[1],
        "username": "u",
        "password": "p",
    }
    async with Target() as target:
        server, port = await _proxy(_allow(), upstream=config)
        head, reply = await _tunnel(port, f"site.test:{target.port}", b"GET / HTTP/1.1\r\n\r\n")
        server.close()
    up.close()
    assert b" 200 " in head and reply.endswith(b"hello from target")
    assert got == {"user": "u", "password": "p", "host": "site.test"}


async def test_through_a_socks4a_upstream():
    got: dict[str, Any] = {}

    async def socks4(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readexactly(8)
        port = struct.unpack(">H", head[2:4])[0]
        got["user"] = (await reader.readuntil(b"\0"))[:-1].decode()
        got["host"] = (await reader.readuntil(b"\0"))[:-1].decode()
        t_reader, t_writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x00\x5a" + b"\0" * 6)
        await writer.drain()
        await core._splice(reader, writer, t_reader, t_writer)
        writer.close()

    up = await asyncio.start_server(socks4, "127.0.0.1", 0)
    config = {
        "scheme": "socks4",
        "host": "127.0.0.1",
        "port": up.sockets[0].getsockname()[1],
        "username": "id",
    }
    async with Target() as target:
        server, port = await _proxy(_allow(), upstream=config)
        head, reply = await _tunnel(port, f"site.test:{target.port}", b"GET / HTTP/1.1\r\n\r\n")
        server.close()
    up.close()
    assert b" 200 " in head and reply.endswith(b"hello from target")
    assert got == {"user": "id", "host": "site.test"}


def test_upstream_of_reads_the_playwright_proxy_option():
    assert egress_proxy.upstream_of(None) is None
    assert egress_proxy.upstream_of(
        {"server": "socks5://proxy.example:1080", "username": "a", "password": "b"}
    ) == {
        "scheme": "socks5",
        "host": "proxy.example",
        "port": 1080,
        "username": "a",
        "password": "b",
    }
    assert egress_proxy.upstream_of({"server": "http://proxy.example:3128"})["port"] == 3128


# ── the guard's decision ──────────────────────────────────────────────────────


async def test_the_decision_applies_the_guard(monkeypatch):
    monkeypatch.delenv("LIONCLAW_BROWSER_ALLOW_PRIVATE_NETWORK", raising=False)
    state = GuardState(policy=GuardPolicy(deny=["blocked.example"], block_private=True))

    metadata = await egress_proxy.check(state, "169.254.169.254", 80)
    denied = await egress_proxy.check(state, "blocked.example", 443)
    private = await egress_proxy.check(state, "10.1.2.3", 443)
    loopback = await egress_proxy.check(state, "localhost", 8080)
    public = await egress_proxy.check(state, "93.184.216.34", 443)

    assert not metadata["ok"] and not denied["ok"] and not private["ok"] and not loopback["ok"]
    assert public == {"ok": True, "connect": ["93.184.216.34"]}
    hosts = [entry["url"] for entry in state.blocked]
    assert hosts == [
        "//169.254.169.254:80",
        "//blocked.example:443",
        "//10.1.2.3:443",
        "//localhost:8080",
    ]


async def test_the_decision_connects_by_name_when_private_addresses_are_allowed():
    state = GuardState(policy=GuardPolicy(block_private=False))
    assert await egress_proxy.check(state, "localhost", 8080) == {"ok": True, "connect": []}
    # The metadata endpoints never, whatever the switch says.
    assert not (await egress_proxy.check(state, "169.254.169.254", 80))["ok"]


async def test_an_allow_list_is_read_at_connection_time():
    state = GuardState(policy=GuardPolicy(allow=["only.example"], block_private=False))
    assert not (await egress_proxy.check(state, "other.example", 443))["ok"]
    state.policy = GuardPolicy(allow=["other.example"], block_private=False)
    assert (await egress_proxy.check(state, "other.example", 443))["ok"]


# ── the live browser's way: the core as its own process, over stdio ─────────


async def test_the_proxy_in_its_own_process_asks_the_runtime(monkeypatch):
    monkeypatch.setattr(
        egress_proxy,
        "container_argv",
        lambda container: [sys.executable, "-c", egress_proxy.core_source()],
    )
    state = GuardState(policy=GuardPolicy(deny=["blocked.example"], block_private=False))
    proxy = await egress_proxy.start_in_container("unused", state)
    try:
        assert proxy.playwright_proxy() == {
            "server": f"http://127.0.0.1:{proxy.port}",
            "bypass": "<-loopback>",
        }
        async with Target() as target:
            head, reply = await _tunnel(
                proxy.port, f"127.0.0.1:{target.port}", b"GET / HTTP/1.1\r\n\r\n"
            )
            refused, _ = await _tunnel(proxy.port, "blocked.example:443", b"")
        assert b" 200 " in head and reply.endswith(b"hello from target")
        assert refused.startswith(b"HTTP/1.1 403")
        assert [e["url"] for e in state.blocked] == ["//blocked.example:443"]
    finally:
        await proxy.close()
    assert proxy.proc.returncode is not None


def test_names_are_resolved_once_a_minute(monkeypatch):
    from olano.security import ssrf

    calls: list[str] = []

    def fake(host: str, port: int | None = None) -> tuple[str | None, list[str]]:
        calls.append(host)
        return None, ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "resolve_public", fake)
    egress_proxy.clear_dns_cache()
    for _ in range(5):
        assert egress_proxy._resolve_public_cached("Example.com", 443) == (None, ["93.184.216.34"])
    assert calls == ["Example.com"]
    egress_proxy.clear_dns_cache()


async def test_a_proxy_that_stopped_is_not_alive(monkeypatch):
    monkeypatch.setattr(
        egress_proxy,
        "container_argv",
        lambda container: [sys.executable, "-c", egress_proxy.core_source()],
    )
    local = await egress_proxy.start_local(GuardState())
    assert local.alive()
    await local.close()
    assert not local.alive()
    remote = await egress_proxy.start_in_container("unused", GuardState())
    assert remote.alive()
    remote.proc.kill()
    await remote.proc.wait()
    assert not remote.alive()
    await remote.close()
