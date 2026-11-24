"""The page-service keeper: one process that owns a page's server and its transport.

A service page (``pages_deploy`` with a ``start`` command) runs an agent-written
HTTP server. The runtime cannot reach it over the network on a managed box: the
runtime sits on the compose network and the sandbox on the docker bridge lane,
which docker isolates from each other on purpose. So every running service gets
exactly one keeper, started with ``docker exec -i <sandbox> python3 -c <this
file> olano-pages-keeper <spec>`` (or as a host subprocess on the open posture,
where there is no sandbox). The keeper:

* starts the service command in its **own process group**, with ``PORT`` set to
  a free loopback port (or the one the agent pinned) and ``HOST=127.0.0.1``;
* writes the service's stdout+stderr to a size-capped, rotated log in the
  workspace, where the agent can read it;
* **multiplexes** TCP connections to ``127.0.0.1:<port>`` over its own stdin and
  stdout, so one ``docker exec`` carries every request the page ever serves:
  no per-request process spawn, no listening socket anywhere outside the
  sandbox, nothing published on the host;
* ties the service's life to its own: stdin EOF (the runtime went away, or the
  docker CLI holding the exec was killed) or a ``SHUTDOWN`` frame stops the whole
  process group, ``SIGTERM`` first and ``SIGKILL`` after a grace period. A
  service never outlives the runtime that supervises it, so there is nothing to
  adopt or reap after a restart. A pidfile is the belt: a keeper whose stdin EOF
  somehow never arrived is found by the next keeper for the same slug and its
  group killed before the new service starts.

The wire format is deliberately small. Every frame is a 9-byte header
(``type: u8``, ``stream: u32``, ``length: u32``, big-endian) and a payload of at
most :data:`MAX_PAYLOAD` bytes. Each stream direction has a credit window
(:data:`INITIAL_WINDOW`); a sender never has more unacknowledged bytes in flight
than the receiver granted, and the receiver grants credit back (``WINDOW``) only
after the bytes reached their consumer. One slow browser download therefore
holds back its own stream and nothing else, and neither side ever buffers more
than a window per stream.

**This module must stay stdlib-only and import nothing from olano**: its source
text is what runs inside the sandbox (``python3 -c``), on whatever Python the
sandbox image ships (3.11 on the bookworm image). The runtime side imports the
constants below from here so the two ends cannot drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MARKER = "olano-pages-keeper"

HEADER = struct.Struct(">BII")
MAX_PAYLOAD = 64 * 1024
INITIAL_WINDOW = 256 * 1024
WINDOW_UPDATE_THRESHOLD = 64 * 1024

# Frame types. k = keeper, r = runtime.
HELLO = 1  # k→r  json {version, port, pid}
OPEN = 2  # r→k  open a connection to the service port on this stream id
OPEN_OK = 3  # k→r
OPEN_FAIL = 4  # k→r  utf-8 reason
DATA = 5  # both
EOF = 6  # both  half-close: no more DATA in this direction
RESET = 7  # both  abort the stream
WINDOW = 8  # both  u32 credit increment
EXIT = 9  # k→r  json {code, stopped}
SHUTDOWN = 10  # r→k  stop the service, then exit
LISTEN = 11  # k→r  json {scope: "loopback" | "all" | "other"}

_CREDIT = struct.Struct(">I")
_OPEN_TIMEOUT = 5.0
_TCP_LISTEN = "0A"


def encode_frame(kind: int, stream: int, payload: bytes = b"") -> bytes:
    """One frame on the wire. Payloads over :data:`MAX_PAYLOAD` are a caller bug."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"frame payload {len(payload)} exceeds {MAX_PAYLOAD}")
    return HEADER.pack(kind, stream, len(payload)) + payload


def encode_credit(n: int) -> bytes:
    return _CREDIT.pack(n)


def decode_credit(payload: bytes) -> int:
    return int(_CREDIT.unpack(payload[:4])[0]) if len(payload) >= 4 else 0


def free_loopback_port() -> int:
    """A port nothing listens on right now (the kernel picks it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def listen_scope(port: int, proc_root: str = "/proc") -> str | None:
    """Which address a LISTEN socket on ``port`` is bound to, read from procfs.

    ``"loopback"`` (127.0.0.0/8 or ::1), ``"all"`` (0.0.0.0 or ::), ``"other"``
    (a specific interface address), or ``None`` when nothing listens yet or
    procfs is unavailable (macOS host lane). A service on ``all`` is reachable
    by every other container on the same docker bridge, around the page's
    password, which is why the runtime surfaces it as a warning.
    """
    found: str | None = None
    for name, width in (("tcp", 8), ("tcp6", 32)):
        try:
            with open(f"{proc_root}/net/{name}", encoding="ascii") as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != _TCP_LISTEN:
                continue
            addr, _, port_hex = fields[1].partition(":")
            try:
                if int(port_hex, 16) != port:
                    continue
            except ValueError:
                continue
            if len(addr) != width:
                continue
            if set(addr) == {"0"}:
                return "all"
            if width == 8:
                # Little-endian IPv4: the first octet is the last byte pair.
                loopback = addr[6:8] == "7F"
            else:
                loopback = addr == "00000000000000000000000001000000" or (
                    # ::ffff:127.x.y.z
                    addr[:24] == "0000000000000000FFFF0000" and addr[30:32] == "7F"
                )
            found = "loopback" if loopback else (found or "other")
    return found


class _RotatingLog:
    """Append-only log capped at ``max_bytes``, keeping one rotated generation."""

    def __init__(self, path: str, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max(64 * 1024, int(max_bytes))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "ab", buffering=0)  # noqa: SIM115 - long-lived handle
        self._size = self._fh.tell()

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self._size + len(data) > self.max_bytes:
            self._fh.close()
            with contextlib.suppress(OSError):
                Path(self.path).replace(self.path + ".1")
            self._fh = open(self.path, "ab", buffering=0)  # noqa: SIM115
            self._size = 0
        self._fh.write(data)
        self._size += len(data)

    def note(self, text: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(f"[olano {stamp}] {text}\n".encode("utf-8", "replace"))

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._fh.close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def reap_stale(pidfile: str, slug: str) -> bool:
    """Kill a previous keeper for ``slug`` (and its service group) if one survives.

    Only a process whose command line still carries this keeper's marker and
    slug is touched, so a recycled PID belonging to something unrelated is
    never signalled. Returns True when something was killed.
    """
    try:
        with open(pidfile, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return False
    killed = False
    keeper_pid = int(record.get("keeper_pid") or 0)
    pgid = int(record.get("pgid") or 0)
    if keeper_pid and keeper_pid != os.getpid() and _alive(keeper_pid):
        cmd = _cmdline(keeper_pid)
        if MARKER in cmd and slug in cmd:
            if pgid > 1:
                with contextlib.suppress(OSError):
                    os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                os.kill(keeper_pid, signal.SIGKILL)
            killed = True
    elif pgid > 1 and not keeper_pid:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
            killed = True
    return killed


class _Stream:
    __slots__ = ("drain", "inbound", "pump", "send_window", "sid", "window_event", "writer")

    def __init__(self, sid: int, writer: asyncio.StreamWriter) -> None:
        self.sid = sid
        self.writer = writer
        self.send_window = INITIAL_WINDOW
        self.window_event = asyncio.Event()
        self.window_event.set()
        # Inbound DATA (runtime → service). Bounded by the window the runtime
        # was granted, so this queue can never hold more than INITIAL_WINDOW.
        self.inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.pump: asyncio.Task[None] | None = None
        self.drain: asyncio.Task[None] | None = None


class Keeper:
    """The keeper's event loop state. ``run()`` returns the process exit code."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.slug = str(spec.get("slug") or "page")
        self.port = int(spec.get("port") or 0) or free_loopback_port()
        self.grace = float(spec.get("stop_grace") or 10.0)
        self.log = _RotatingLog(
            str(spec.get("log_path") or "service.log"),
            int(spec.get("log_max_bytes") or 2 * 1024 * 1024),
        )
        self.pidfile = str(spec.get("pidfile") or "")
        self.streams: dict[int, _Stream] = {}
        self.proc: asyncio.subprocess.Process | None = None
        self.out: asyncio.StreamWriter | None = None
        self._out_lock = asyncio.Lock()
        self._stopping = False
        self._stopped_by_request = False

    # ── wire ─────────────────────────────────────────────────────────────────

    async def send(self, kind: int, sid: int, payload: bytes = b"") -> None:
        out = self.out
        if out is None or out.is_closing():
            return
        # One write per frame keeps frames whole; drain under a lock because
        # several stream pumps drain concurrently.
        out.write(encode_frame(kind, sid, payload))
        async with self._out_lock:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await out.drain()

    async def send_json(self, kind: int, payload: dict[str, Any]) -> None:
        await self.send(kind, 0, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    # ── service process ──────────────────────────────────────────────────────

    def _service_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key, value in (self.spec.get("env") or {}).items():
            env[str(key)] = str(value)
        env["PORT"] = str(self.port)
        env["HOST"] = "127.0.0.1"
        return env

    async def _start_service(self) -> None:
        command = str(self.spec.get("command") or "")
        cwd = str(self.spec.get("cwd") or ".")
        self.log.note(f"starting: {command} (cwd {cwd}, PORT={self.port})")
        self.proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=cwd,
            env=self._service_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        if self.pidfile:
            record = {
                "keeper_pid": os.getpid(),
                "service_pid": self.proc.pid,
                "pgid": self.proc.pid,
                "slug": self.slug,
                "port": self.port,
                "started": time.time(),
            }
            tmp = self.pidfile + ".tmp"
            try:
                Path(self.pidfile).parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(record, fh)
                Path(tmp).replace(self.pidfile)
            except OSError as exc:
                print(f"keeper: could not write pidfile: {exc}", file=sys.stderr)

    async def _pump_output(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                return
            self.log.write(chunk)

    def _signal_group(self, sig: int) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)

    async def stop_service(self) -> int | None:
        """SIGTERM the group, SIGKILL after the grace period; the exit code."""
        proc = self.proc
        if proc is None:
            return None
        if proc.returncode is None:
            self._signal_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.grace)
            except TimeoutError:
                self.log.note(f"still running {self.grace:g}s after SIGTERM, sending SIGKILL")
                self._signal_group(signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5)
        # Children that ignored SIGTERM but stayed in the group.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        return proc.returncode

    async def _watch_listen(self) -> None:
        reported: str | None = None
        delay = 0.25
        while True:
            scope = listen_scope(self.port)
            if scope is not None and scope != reported:
                reported = scope
                await self.send_json(LISTEN, {"scope": scope})
            await asyncio.sleep(delay)
            delay = min(5.0, delay * 2)

    # ── streams ──────────────────────────────────────────────────────────────

    async def _open(self, sid: int) -> None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.port), timeout=_OPEN_TIMEOUT
            )
        except (TimeoutError, OSError) as exc:
            await self.send(OPEN_FAIL, sid, str(exc).encode("utf-8", "replace")[:512])
            return
        stream = _Stream(sid, writer)
        self.streams[sid] = stream
        stream.pump = asyncio.ensure_future(self._pump_stream(stream, reader))
        stream.drain = asyncio.ensure_future(self._drain_stream(stream))
        await self.send(OPEN_OK, sid)

    async def _pump_stream(self, stream: _Stream, reader: asyncio.StreamReader) -> None:
        """Service socket → runtime, never past the credit the runtime granted."""
        try:
            while True:
                while stream.send_window <= 0:
                    stream.window_event.clear()
                    await stream.window_event.wait()
                chunk = await reader.read(min(MAX_PAYLOAD, stream.send_window))
                if not chunk:
                    await self.send(EOF, stream.sid)
                    return
                stream.send_window -= len(chunk)
                await self.send(DATA, stream.sid, chunk)
        except (ConnectionError, OSError):
            await self._reset(stream.sid, notify=True)
        except asyncio.CancelledError:
            raise

    async def _drain_stream(self, stream: _Stream) -> None:
        """Runtime → service socket; credit goes back only once bytes are written."""
        pending = 0
        try:
            while True:
                chunk = await stream.inbound.get()
                if chunk is None:
                    if stream.writer.can_write_eof():
                        stream.writer.write_eof()
                    return
                stream.writer.write(chunk)
                await stream.writer.drain()
                pending += len(chunk)
                if pending >= WINDOW_UPDATE_THRESHOLD or stream.inbound.empty():
                    await self.send(WINDOW, stream.sid, encode_credit(pending))
                    pending = 0
        except (ConnectionError, OSError):
            await self._reset(stream.sid, notify=True)

    async def _reset(self, sid: int, *, notify: bool) -> None:
        stream = self.streams.pop(sid, None)
        if stream is None:
            return
        current = asyncio.current_task()
        for task in (stream.pump, stream.drain):
            if task is not None and task is not current and not task.done():
                task.cancel()
        with contextlib.suppress(Exception):
            stream.writer.close()
        if notify:
            await self.send(RESET, sid)

    async def _handle(self, kind: int, sid: int, payload: bytes) -> bool:
        """Dispatch one inbound frame; False means stop the keeper."""
        if kind == OPEN:
            if sid in self.streams:
                await self.send(OPEN_FAIL, sid, b"stream id in use")
            else:
                asyncio.ensure_future(self._open(sid))
        elif kind == DATA:
            stream = self.streams.get(sid)
            if stream is not None:
                stream.inbound.put_nowait(payload)
        elif kind == EOF:
            stream = self.streams.get(sid)
            if stream is not None:
                stream.inbound.put_nowait(None)
        elif kind == WINDOW:
            stream = self.streams.get(sid)
            if stream is not None:
                stream.send_window += decode_credit(payload)
                stream.window_event.set()
        elif kind == RESET:
            await self._reset(sid, notify=False)
        elif kind == SHUTDOWN:
            self._stopped_by_request = True
            return False
        # Unknown frame types are ignored: a newer runtime may send more.
        return True

    async def _read_frames(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                head = await reader.readexactly(HEADER.size)
            except (asyncio.IncompleteReadError, ConnectionError):
                return  # stdin EOF: the runtime is gone
            kind, sid, length = HEADER.unpack(head)
            if length > MAX_PAYLOAD:
                print(f"keeper: oversized frame ({length} bytes), stopping", file=sys.stderr)
                return
            try:
                payload = await reader.readexactly(length) if length else b""
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            if not await self._handle(kind, sid, payload):
                return

    # ── main ─────────────────────────────────────────────────────────────────

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        stdin_reader = asyncio.StreamReader(limit=2 * MAX_PAYLOAD)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stdin_reader), os.fdopen(0, "rb", buffering=0)
        )
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(1, "wb", buffering=0)
        )
        self.out = asyncio.StreamWriter(transport, protocol, None, loop)

        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)

        if self.pidfile and reap_stale(self.pidfile, self.slug):
            self.log.note("stopped a previous keeper for this page that was still running")

        try:
            await self._start_service()
        except OSError as exc:
            self.log.note(f"could not start: {exc}")
            await self.send_json(EXIT, {"code": 127, "stopped": False, "error": str(exc)})
            return 0
        await self.send_json(
            HELLO, {"version": PROTOCOL_VERSION, "port": self.port, "pid": os.getpid()}
        )

        assert self.proc is not None
        output = asyncio.ensure_future(self._pump_output())
        frames = asyncio.ensure_future(self._read_frames(stdin_reader))
        exited = asyncio.ensure_future(self.proc.wait())
        signalled = asyncio.ensure_future(stop.wait())
        listen = asyncio.ensure_future(self._watch_listen())

        await asyncio.wait({frames, exited, signalled}, return_when=asyncio.FIRST_COMPLETED)
        listen.cancel()

        stopped = not exited.done()
        if stopped:
            reason = "stop requested" if self._stopped_by_request else "runtime went away"
            if signalled.done():
                reason = "keeper signalled"
            self.log.note(f"stopping ({reason})")
        code = await self.stop_service()
        # Drain what the service printed on its way out before the log closes.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(output, timeout=2)
        if not stopped:
            self.log.note(f"exited with code {code}")
        for sid in list(self.streams):
            await self._reset(sid, notify=True)
        await self.send_json(EXIT, {"code": code, "stopped": stopped})
        for task in (frames, signalled, exited):
            task.cancel()
        if self.pidfile:
            with contextlib.suppress(OSError):
                Path(self.pidfile).unlink()
        self.log.close()
        if self.out is not None:
            with contextlib.suppress(Exception):
                self.out.close()
        return 0


def main(argv: list[str]) -> int:
    # argv: [<-c placeholder>, MARKER, <spec json>]. The marker sits on the
    # command line so reap_stale() can recognise a keeper by /proc cmdline.
    if len(argv) < 3 or argv[1] != MARKER:
        print(f"usage: python3 -c <keeper source> {MARKER} <spec-json>", file=sys.stderr)
        return 2
    try:
        spec = json.loads(argv[2])
    except ValueError as exc:
        print(f"keeper: bad spec: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(Keeper(spec).run())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
