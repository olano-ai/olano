"""The browser's egress proxy: one connection at a time, asked about first.

Standard library only, with no import from the rest of the package, because
the same source runs in two places (:mod:`olano.browser.egress_proxy`): in the
runtime, next to the shared headless Chromium, and inside an agent's live
``olano-browser`` container, sent there over ``docker exec`` as ``python3 -c``
so the published image needs nothing new (the browser in the container exits
from the container, and so must its proxy, or the site would see another
address).

Chromium hands it every connection a context makes: ``CONNECT host:port`` for
HTTPS and for every WebSocket (``ws://`` included), an absolute-form request
(``GET http://host/path``) for plain HTTP. Before connecting it asks
``decide(host, port)``, which answers ``{"ok": true, "connect": [addresses]}``
(the addresses the decision checked, tried in order, so a name cannot resolve
to something else between the check and the connection; empty means connect
by name) or ``{"ok": false, "reason": ..., "status": ...}`` (the client gets
``status``, ``403`` by default). With an ``upstream`` proxy (the agent's
``BROWSER_PROXY_URL``) the connection goes through it by name instead:
``http``/``https`` with ``CONNECT``, ``socks5`` and ``socks4`` (4a), with the
upstream's own sign-in.

It exists because the egress guard cannot see a WebSocket any other way
without putting something on the page: Chromium's own request interception
(DevTools ``Fetch``) never pauses a WebSocket handshake, and Playwright's
``route_web_socket`` works by adding a binding to every page, which is what
made Cloudflare challenge the agent's browser (browser/guard.py).
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import ssl
import struct
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urlsplit

HEAD_LIMIT = 64 * 1024
CONNECT_TIMEOUT_S = 30.0
HEAD_TIMEOUT_S = 30.0
CHUNK = 64 * 1024
# Headers that describe the hop to this proxy, never sent on.
_HOP_HEADERS = frozenset(
    {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "trailer"}
)

Decide = Callable[[str, int], Awaitable[dict[str, Any]]]


class UpstreamError(OSError):
    """The upstream proxy refused or failed the connection."""


def split_host_port(target: str, default_port: int) -> tuple[str, int]:
    """``host:port`` (``[v6]:port`` too) as ``(host, port)``."""
    target = target.strip()
    if target.startswith("["):
        end = target.find("]")
        if end < 0:
            raise ValueError("bad host")
        host, rest = target[1:end], target[end + 1 :]
        port = int(rest[1:]) if rest.startswith(":") else default_port
    elif target.count(":") == 1:
        host, _, port_text = target.partition(":")
        port = int(port_text)
    else:
        host, port = target, default_port
    if not host or not 0 < port < 65536:
        raise ValueError("bad host or port")
    return host, port


def _host_for_request(host: str) -> str:
    try:
        return f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        return host


async def _reply(writer: asyncio.StreamWriter, status: str, body: str = "") -> None:
    data = body.encode("utf-8", "replace")
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
        + data
    )
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (ConnectionError, OSError, RuntimeError):
            pass


async def _splice(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    await asyncio.gather(_pump(a_reader, b_writer), _pump(b_reader, a_writer))


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)


async def _via_http_proxy(
    upstream: dict[str, Any], host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await _open_http_upstream(upstream)
    target = f"{_host_for_request(host)}:{port}"
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    auth = _proxy_authorization(upstream)
    if auth:
        lines.append(auth)
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    head = await _read_head(reader)
    status = head.split(b"\r\n", 1)[0].split(b" ")
    if len(status) < 2 or status[1] != b"200":
        await _close(writer)
        raise UpstreamError(f"upstream proxy answered {b' '.join(status[1:]).decode('latin-1')}")
    return reader, writer


async def _via_socks(
    upstream: dict[str, Any], host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(upstream["host"], upstream["port"]), CONNECT_TIMEOUT_S
    )
    try:
        if upstream["scheme"] == "socks4":
            user_id = str(upstream.get("username") or "").encode()
            writer.write(
                struct.pack(">BBH", 4, 1, port)
                + bytes([0, 0, 0, 1])
                + user_id
                + b"\0"
                + host.encode("idna")
                + b"\0"
            )
            await writer.drain()
            answer = await asyncio.wait_for(reader.readexactly(8), CONNECT_TIMEOUT_S)
            if answer[1] != 0x5A:
                raise UpstreamError(f"SOCKS4 proxy refused the connection ({answer[1]})")
            return reader, writer
        user = str(upstream.get("username") or "")
        methods = b"\x00\x02" if user else b"\x00"
        writer.write(bytes([5, len(methods)]) + methods)
        await writer.drain()
        chosen = await asyncio.wait_for(reader.readexactly(2), CONNECT_TIMEOUT_S)
        if chosen[1] == 2:
            name, secret = user.encode(), str(upstream.get("password") or "").encode()
            writer.write(bytes([1, len(name)]) + name + bytes([len(secret)]) + secret)
            await writer.drain()
            if (await asyncio.wait_for(reader.readexactly(2), CONNECT_TIMEOUT_S))[1] != 0:
                raise UpstreamError("SOCKS5 proxy refused the sign-in")
        elif chosen[1] != 0:
            raise UpstreamError("SOCKS5 proxy accepts none of the offered sign-in methods")
        name = host.encode("idna")
        writer.write(bytes([5, 1, 0, 3, len(name)]) + name + struct.pack(">H", port))
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(4), CONNECT_TIMEOUT_S)
        if head[1] != 0:
            raise UpstreamError(f"SOCKS5 proxy refused the connection ({head[1]})")
        skip = {1: 4, 4: 16}.get(head[3])
        if skip is None:
            skip = (await reader.readexactly(1))[0]
        await reader.readexactly(skip + 2)
        return reader, writer
    except Exception:
        await _close(writer)
        raise


async def open_target(
    host: str, port: int, connect: list[str] | None, upstream: dict[str, Any] | None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to *host*:*port*: through *upstream* by name when there is
    one, else to the first of *connect* (the checked addresses) that answers,
    or to *host* by name when *connect* is empty."""
    if upstream:
        if upstream["scheme"] in ("socks5", "socks4"):
            return await _via_socks(upstream, host, port)
        return await _via_http_proxy(upstream, host, port)
    error: BaseException | None = None
    for address in connect or [host]:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT_S)
        except (OSError, TimeoutError) as exc:
            error = exc
    raise error if isinstance(error, OSError) else OSError(str(error or "no address"))


def _proxy_authorization(upstream: dict[str, Any]) -> str | None:
    if not upstream.get("username"):
        return None
    token = f"{upstream['username']}:{upstream.get('password', '')}".encode()
    return "Proxy-Authorization: Basic " + base64.b64encode(token).decode()


async def _open_http_upstream(
    upstream: dict[str, Any],
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    ctx = ssl.create_default_context() if upstream["scheme"] == "https" else None
    return await asyncio.wait_for(
        asyncio.open_connection(
            upstream["host"],
            upstream["port"],
            ssl=ctx,
            server_hostname=upstream["host"] if ctx else None,
        ),
        CONNECT_TIMEOUT_S,
    )


def _absolute_form_head(lines: list[str], upstream: dict[str, Any]) -> bytes:
    """A plain-HTTP request for an upstream HTTP proxy, as Chromium would send
    it: absolute form, the upstream's own sign-in, never ours."""
    out = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        if line.split(":", 1)[0].strip().lower() in _HOP_HEADERS:
            continue
        out.append(line)
    auth = _proxy_authorization(upstream)
    if auth:
        out.append(auth)
    out.append("Connection: close")
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")


def _origin_form_head(lines: list[str], path: str, host: str, port: int) -> bytes:
    method, _, rest = lines[0].partition(" ")
    version = rest.rsplit(" ", 1)[-1] if " " in rest else "HTTP/1.1"
    out = [f"{method} {path} {version}"]
    has_host = False
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(":", 1)[0].strip().lower()
        if name in _HOP_HEADERS:
            continue
        has_host = has_host or name == "host"
        out.append(line)
    if not has_host:
        out.append(f"Host: {_host_for_request(host)}" + ("" if port == 80 else f":{port}"))
    out.append("Connection: close")
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")


def handler(
    decide: Decide, upstream: dict[str, Any] | None
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target_writer: asyncio.StreamWriter | None = None
        try:
            try:
                head = await _read_head(reader)
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError):
                return
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3:
                await _reply(writer, "400 Bad Request")
                return
            method, target = parts[0].upper(), parts[1]
            try:
                if method == "CONNECT":
                    host, port = split_host_port(target, 443)
                    path = ""
                else:
                    url = urlsplit(target)
                    if url.scheme.lower() != "http" or not url.hostname:
                        await _reply(writer, "400 Bad Request", "Only http:// is forwarded.")
                        return
                    host, port = unquote(url.hostname), url.port or 80
                    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            except ValueError:
                await _reply(writer, "400 Bad Request")
                return
            try:
                verdict = await decide(host, port)
            except Exception as exc:  # fail closed
                verdict = {"ok": False, "reason": f"Connection check failed: {exc}"}
            if not verdict.get("ok"):
                await _reply(
                    writer,
                    str(verdict.get("status") or "403 Forbidden"),
                    str(verdict.get("reason") or "Refused."),
                )
                return
            # Plain HTTP through an HTTP upstream goes as Chromium would send it:
            # many proxies refuse CONNECT to any port but 443.
            forward = (
                method != "CONNECT"
                and upstream is not None
                and upstream["scheme"] in ("http", "https")
            )
            try:
                if forward:
                    assert upstream is not None
                    target_reader, target_writer = await _open_http_upstream(upstream)
                else:
                    connect = verdict.get("connect") or []
                    target_reader, target_writer = await open_target(
                        host, port, [str(a) for a in connect], upstream
                    )
            except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
                await _reply(writer, "502 Bad Gateway", str(exc)[:300])
                return
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            elif forward:
                assert upstream is not None
                target_writer.write(_absolute_form_head(lines, upstream))
                await target_writer.drain()
            else:
                target_writer.write(_origin_form_head(lines, path, host, port))
                await target_writer.drain()
            await _splice(reader, writer, target_reader, target_writer)
        except (ConnectionError, OSError):
            pass
        finally:
            await _close(target_writer)
            await _close(writer)

    return _handle


async def serve(
    decide: Decide, *, upstream: dict[str, Any] | None = None, host: str = "127.0.0.1"
) -> asyncio.base_events.Server:
    """Listen on *host* (a free port); the bound port is ``server.sockets[0]``."""
    return await asyncio.start_server(handler(decide, upstream), host, 0, limit=HEAD_LIMIT)


# ── running inside the browser's container (python3 -c) ──────────────────────


async def _stdio_main() -> None:
    """Config on the first stdin line, the bound port on the first stdout
    line, then one JSON line out per decision asked and one in per answer.
    Ends when stdin closes (the runtime went away)."""
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=HEAD_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
    config = json.loads((await stdin.readline()).decode() or "{}")
    waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
    counter = 0

    def _say(payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    async def decide(host: str, port: int) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        ask = counter
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        waiting[ask] = future
        _say({"id": ask, "host": host, "port": port})
        try:
            return await asyncio.wait_for(future, CONNECT_TIMEOUT_S)
        finally:
            waiting.pop(ask, None)

    server = await serve(decide, upstream=config.get("upstream"))
    _say({"port": server.sockets[0].getsockname()[1]})
    while True:
        line = await stdin.readline()
        if not line:
            break
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        future = waiting.get(int(answer.get("id", 0)))
        if future is not None and not future.done():
            future.set_result(answer)
    server.close()
    for future in waiting.values():
        if not future.done():
            future.set_result({"ok": False, "reason": "The runtime went away."})


if __name__ == "__main__":
    asyncio.run(_stdio_main())
