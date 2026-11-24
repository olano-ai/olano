"""The page-service keeper and its mux transport, end to end on the host lane.

These run the real keeper source as a subprocess (exactly what the sandbox
lane runs through ``docker exec``) in front of a real HTTP server, and talk to
it through the real httpcore pool. Nothing here is mocked except the absence
of a container.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

import httpcore
import pytest

from olano.pages import keeper as k
from olano.pages.lanes import keeper_source
from olano.pages.mux import MuxClosed, MuxNetworkBackend, MuxSession

SERVER = textwrap.dedent(
    """
    import http.server, json, os, sys

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            sys.stdout.write("req %s\\n" % self.path); sys.stdout.flush()

        def _send(self, code, body, ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/big/"):
                n = int(self.path.rsplit("/", 1)[1])
                self._send(200, b"z" * n)
            elif self.path == "/headers":
                self._send(200, json.dumps(dict(self.headers)).encode(), "application/json")
            elif self.path == "/crash":
                os._exit(3)
            else:
                self._send(200, b"ok " + self.path.encode())

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n)
            self._send(200, str(len(data)).encode() + b":" + data[:16])

    http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
    """
)


def _spec(tmp_path: Path, command: str, **extra) -> dict:
    (tmp_path / "server.py").write_text(SERVER)
    spec = {
        "slug": "demo",
        "cwd": str(tmp_path),
        "command": command,
        "log_path": str(tmp_path / "state" / "service.log"),
        "pidfile": str(tmp_path / "state" / "keeper.pid"),
        "stop_grace": 3,
    }
    spec.update(extra)
    return spec


async def _spawn(spec: dict) -> MuxSession:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        keeper_source(),
        k.MARKER,
        json.dumps(spec),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return MuxSession(proc, label="test")


async def _ready(pool: httpcore.AsyncConnectionPool, port: int) -> None:
    for _ in range(100):
        try:
            r = await pool.request("GET", f"http://127.0.0.1:{port}/")
            if r.status == 200:
                return
        except httpcore.ConnectError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("service never answered")


@pytest.fixture
def server_cmd() -> str:
    return f"exec {sys.executable} server.py"


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_frame_roundtrip_and_payload_cap():
    frame = k.encode_frame(k.DATA, 7, b"abc")
    kind, sid, length = k.HEADER.unpack(frame[: k.HEADER.size])
    assert (kind, sid, length) == (k.DATA, 7, 3)
    assert frame[k.HEADER.size :] == b"abc"
    assert k.decode_credit(k.encode_credit(123456)) == 123456
    with pytest.raises(ValueError):
        k.encode_frame(k.DATA, 1, b"x" * (k.MAX_PAYLOAD + 1))


def _proc_net(tmp_path: Path, tcp: list[str], tcp6: list[str] | None = None) -> str:
    net = tmp_path / "proc" / "net"
    net.mkdir(parents=True)
    head = "  sl  local_address rem_address   st\n"
    (net / "tcp").write_text(head + "".join(tcp))
    (net / "tcp6").write_text(head + "".join(tcp6 or []))
    return str(tmp_path / "proc")


def test_listen_scope_reads_procfs(tmp_path):
    # 127.0.0.1:8080 listening; port 0x1F90 = 8080.
    root = _proc_net(tmp_path / "a", ["   0: 0100007F:1F90 00000000:0000 0A x\n"])
    assert k.listen_scope(8080, root) == "loopback"
    root = _proc_net(tmp_path / "b", ["   0: 00000000:1F90 00000000:0000 0A x\n"])
    assert k.listen_scope(8080, root) == "all"
    # An established (01) socket on the port is not a listener.
    root = _proc_net(tmp_path / "c", ["   0: 00000000:1F90 0100007F:9999 01 x\n"])
    assert k.listen_scope(8080, root) is None
    # IPv6 ::1 and :: .
    root = _proc_net(tmp_path / "d", [], ["   0: 00000000000000000000000001000000:1F90 0:0 0A x\n"])
    assert k.listen_scope(8080, root) == "loopback"
    root = _proc_net(tmp_path / "e", [], ["   0: 00000000000000000000000000000000:1F90 0:0 0A x\n"])
    assert k.listen_scope(8080, root) == "all"
    # A specific non-loopback address (10.0.0.5).
    root = _proc_net(tmp_path / "f", ["   0: 0500000A:1F90 00000000:0000 0A x\n"])
    assert k.listen_scope(8080, root) == "other"
    assert k.listen_scope(8080, str(tmp_path / "missing")) is None


def test_reap_stale_never_touches_an_unrelated_pid(tmp_path):
    # Our own test process: alive, but its cmdline carries no keeper marker.
    pidfile = tmp_path / "keeper.pid"
    pidfile.write_text(json.dumps({"keeper_pid": os.getppid(), "pgid": 0}))
    assert k.reap_stale(str(pidfile), "demo") is False
    assert k.reap_stale(str(tmp_path / "absent.pid"), "demo") is False


def test_rotating_log_caps_size(tmp_path):
    log = k._RotatingLog(str(tmp_path / "s.log"), 64 * 1024)
    for _ in range(100):
        log.write(b"x" * 1024)
    log.close()
    assert (tmp_path / "s.log").stat().st_size <= 64 * 1024
    assert (tmp_path / "s.log.1").exists()


def test_keeper_source_is_stdlib_only():
    """The source runs in the sandbox, which has no olano installed."""
    import ast

    tree = ast.parse(keeper_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module and not node.module.startswith("olano"), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("olano"), alias.name


# ── end to end ───────────────────────────────────────────────────────────────


async def test_requests_flow_through_one_keeper(tmp_path, server_cmd):
    session = await _spawn(_spec(tmp_path, server_cmd))
    hello = await session.wait_hello(15)
    port = hello["port"]
    assert hello["version"] == k.PROTOCOL_VERSION
    pool = httpcore.AsyncConnectionPool(
        network_backend=MuxNetworkBackend(session), max_connections=8
    )
    try:
        await _ready(pool, port)
        r = await pool.request("GET", f"http://127.0.0.1:{port}/hello?x=1")
        assert r.content == b"ok /hello?x=1"
        # Many streams at once, each far larger than one credit window, so
        # flow control has to work in the keeper → runtime direction.
        size = 3 * k.INITIAL_WINDOW + 17
        results = await asyncio.gather(
            *(pool.request("GET", f"http://127.0.0.1:{port}/big/{size}") for _ in range(6))
        )
        assert all(len(x.content) == size for x in results)
        # And the runtime → keeper direction: a body bigger than a window.
        body = os.urandom(2 * k.INITIAL_WINDOW + 5)
        r = await pool.request(
            "POST",
            f"http://127.0.0.1:{port}/upload",
            headers={"Content-Length": str(len(body))},
            content=body,
        )
        assert r.content == str(len(body)).encode() + b":" + body[:16]
        # A slow consumer still gets every byte.
        async with pool.stream("GET", f"http://127.0.0.1:{port}/big/{size}") as resp:
            got = 0
            async for chunk in resp.aiter_stream():
                got += len(chunk)
                await asyncio.sleep(0.001)
        assert got == size
        for _ in range(50):
            if session.listen_scope:
                break
            await asyncio.sleep(0.05)
        assert session.listen_scope == "loopback"
    finally:
        await pool.aclose()
        info = await session.shutdown(grace=10)
    assert info["stopped"] is True
    log = (tmp_path / "state" / "service.log").read_text()
    assert "starting:" in log and "req /hello?x=1" in log and "stopping (stop requested)" in log
    assert not (tmp_path / "state" / "keeper.pid").exists()


async def test_service_crash_reports_exit_code(tmp_path, server_cmd):
    session = await _spawn(_spec(tmp_path, server_cmd))
    port = (await session.wait_hello(15))["port"]
    pool = httpcore.AsyncConnectionPool(network_backend=MuxNetworkBackend(session))
    await _ready(pool, port)
    # The socket EOF and the keeper's stream reset race: the request fails as
    # "disconnected without a response" or as "reset", both correct.
    with pytest.raises((httpcore.RemoteProtocolError, httpcore.ReadError)):
        await pool.request("GET", f"http://127.0.0.1:{port}/crash")
    info = await asyncio.wait_for(asyncio.shield(session.closed), 15)
    assert info["code"] == 3 and info["stopped"] is False
    with pytest.raises(MuxClosed):
        await session.open_stream()
    await pool.aclose()
    await session.shutdown(grace=2)


async def test_command_that_cannot_start_reports_exit(tmp_path):
    session = await _spawn(_spec(tmp_path, "exec /nonexistent/binary-for-test"))
    await session.wait_hello(15)
    info = await asyncio.wait_for(asyncio.shield(session.closed), 15)
    assert info["code"] == 127


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it is dead for our purposes.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


async def test_stdin_eof_kills_the_whole_process_group(tmp_path):
    """The runtime vanishing (its end of the pipe closing) stops the service and
    every child it started, including one that ignores SIGTERM."""
    script = tmp_path / "tree.sh"
    script.write_text(
        "#!/bin/sh\n"
        "sh -c 'trap \"\" TERM; while :; do sleep 1; done' &\n"
        "echo $! > child.pid\n"
        f"exec {sys.executable} server.py\n"
    )
    session = await _spawn(_spec(tmp_path, "sh tree.sh", stop_grace=1))
    await session.wait_hello(15)
    for _ in range(100):
        if (tmp_path / "child.pid").exists() and (tmp_path / "child.pid").read_text().strip():
            break
        await asyncio.sleep(0.05)
    child = int((tmp_path / "child.pid").read_text())
    keeper_pid = session.process.pid
    assert _alive(child)
    # Close our end of stdin without sending SHUTDOWN: what a SIGKILLed
    # runtime (or docker CLI) looks like from the keeper's side.
    session.process.stdin.close()
    await asyncio.wait_for(session.process.wait(), 20)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _alive(child):
        await asyncio.sleep(0.1)
    assert not _alive(child), "a SIGTERM-ignoring child outlived its keeper"
    assert not _alive(keeper_pid)


async def test_keeper_sigterm_stops_service(tmp_path, server_cmd):
    session = await _spawn(_spec(tmp_path, server_cmd))
    await session.wait_hello(15)
    pid = json.loads((tmp_path / "state" / "keeper.pid").read_text())["service_pid"]
    session.process.send_signal(signal.SIGTERM)
    info = await asyncio.wait_for(asyncio.shield(session.closed), 20)
    assert info["stopped"] is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        await asyncio.sleep(0.05)
    assert not _alive(pid)


async def test_open_to_a_port_nobody_listens_on_fails_fast(tmp_path):
    # `sleep` never listens: OPEN must come back refused, not hang.
    session = await _spawn(_spec(tmp_path, "exec sleep 30"))
    await session.wait_hello(15)
    started = time.monotonic()
    with pytest.raises(ConnectionRefusedError):
        await session.open_stream(timeout=10)
    assert time.monotonic() - started < 6
    await session.shutdown(grace=5)
