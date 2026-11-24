"""Runtime side of the keeper transport: one mux session per running page service.

:mod:`olano.pages.keeper` multiplexes connections to the service port over its
stdin/stdout. :class:`MuxSession` is the other end: it owns the keeper process
(a ``docker exec -i`` or a host subprocess), reads frames, and hands out
:class:`MuxStream` objects that behave like a TCP connection to the service.

:class:`MuxNetworkBackend` plugs those streams into httpcore's connection pool,
so the proxy gets HTTP/1.1 keep-alive, connection reuse and correct
idle-connection detection from a maintained HTTP implementation rather than a
hand-rolled one. No socket listens anywhere for this: a request reaches the
service only through a frame the runtime itself writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import Any

import httpcore

from olano.pages.keeper import (
    DATA,
    EOF,
    EXIT,
    HEADER,
    HELLO,
    INITIAL_WINDOW,
    LISTEN,
    MAX_PAYLOAD,
    OPEN,
    OPEN_FAIL,
    OPEN_OK,
    PROTOCOL_VERSION,
    RESET,
    SHUTDOWN,
    WINDOW,
    WINDOW_UPDATE_THRESHOLD,
    decode_credit,
    encode_credit,
    encode_frame,
)

logger = logging.getLogger(__name__)

_MAX_STREAM_ID = 2**32 - 1
_STDERR_TAIL = 40


class MuxClosed(ConnectionError):
    """The keeper is gone (exited, killed, or never said hello)."""


class StreamReset(ConnectionError):
    """The other end aborted this stream."""


class MuxStream:
    """One multiplexed connection to the service port."""

    def __init__(self, session: MuxSession, sid: int) -> None:
        self._session = session
        self.sid = sid
        self._buf = bytearray()
        self._readable = asyncio.Event()
        self._eof = False
        self._error: BaseException | None = None
        self._send_window = INITIAL_WINDOW
        self._window_open = asyncio.Event()
        self._window_open.set()
        self._unacked = 0
        self._local_eof = False
        self._closed = False
        self.opened: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    # ── called by the session's reader ──────────────────────────────────────

    def _feed(self, data: bytes) -> None:
        self._buf.extend(data)
        self._readable.set()

    def _feed_eof(self) -> None:
        self._eof = True
        self._readable.set()

    def _fail(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = exc
        if not self.opened.done():
            self.opened.set_exception(exc)
            # Retrieved here so an unawaited open never logs "exception never
            # retrieved" when the session dies under it.
            self.opened.exception()
        self._readable.set()
        self._window_open.set()

    def _credit(self, n: int) -> None:
        self._send_window += n
        if self._send_window > 0:
            self._window_open.set()

    # ── public, connection-like API ─────────────────────────────────────────

    @property
    def readable(self) -> bool:
        """Data, EOF or an error is waiting: a read would not block."""
        return bool(self._buf) or self._eof or self._error is not None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Up to ``max_bytes``; ``b""`` at EOF. Raises on reset or timeout."""
        while not self._buf and not self._eof and self._error is None:
            self._readable.clear()
            if timeout is None:
                await self._readable.wait()
            else:
                await asyncio.wait_for(self._readable.wait(), timeout)
        if self._buf:
            chunk = bytes(self._buf[:max_bytes])
            del self._buf[: len(chunk)]
            self._unacked += len(chunk)
            if self._unacked >= WINDOW_UPDATE_THRESHOLD or not self._buf:
                credit, self._unacked = self._unacked, 0
                self._session._send(WINDOW, self.sid, encode_credit(credit))
            return chunk
        if self._error is not None:
            raise self._error
        return b""

    async def write(self, data: bytes, timeout: float | None = None) -> None:
        if self._local_eof:
            raise StreamReset("write after EOF")
        view = memoryview(data)
        while view:
            if self._error is not None:
                raise self._error
            while self._send_window <= 0:
                self._window_open.clear()
                if timeout is None:
                    await self._window_open.wait()
                else:
                    await asyncio.wait_for(self._window_open.wait(), timeout)
                if self._error is not None:
                    raise self._error
            n = min(len(view), MAX_PAYLOAD, self._send_window)
            self._send_window -= n
            self._session._send(DATA, self.sid, bytes(view[:n]))
            view = view[n:]
            await self._session._drain()

    def write_eof(self) -> None:
        if not self._local_eof and self._error is None:
            self._local_eof = True
            self._session._send(EOF, self.sid)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._error is None and self._session.alive:
            self._session._send(RESET, self.sid)
        self._fail(StreamReset("closed locally"))
        self._session._streams.pop(self.sid, None)


class MuxSession:
    """Owns one keeper process and its frame stream."""

    def __init__(self, process: asyncio.subprocess.Process, *, label: str = "") -> None:
        if process.stdin is None or process.stdout is None:
            raise ValueError("the keeper process needs piped stdin and stdout")
        self.process = process
        self.label = label or f"pid {process.pid}"
        self.hello: dict[str, Any] = {}
        self.listen_scope: str | None = None
        self.exit_info: dict[str, Any] | None = None
        self.stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)
        self._streams: dict[int, MuxStream] = {}
        self._next_id = 1
        self._hello = asyncio.get_running_loop().create_future()
        self.closed: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._drain_lock = asyncio.Lock()
        self._reader = asyncio.ensure_future(self._read_loop())
        self._stderr = (
            asyncio.ensure_future(self._read_stderr()) if process.stderr is not None else None
        )

    @property
    def alive(self) -> bool:
        return not self.closed.done()

    async def wait_hello(self, timeout: float) -> dict[str, Any]:
        """The keeper's HELLO, or raise :class:`MuxClosed` (it died first)."""
        try:
            return await asyncio.wait_for(asyncio.shield(self._hello), timeout)
        except TimeoutError:
            raise MuxClosed(f"keeper said nothing within {timeout:g}s") from None

    # ── frames out ──────────────────────────────────────────────────────────

    def _send(self, kind: int, sid: int, payload: bytes = b"") -> None:
        stdin = self.process.stdin
        if stdin is None or stdin.is_closing() or not self.alive:
            return
        try:
            stdin.write(encode_frame(kind, sid, payload))
        except (ConnectionError, RuntimeError):
            pass

    async def _drain(self) -> None:
        stdin = self.process.stdin
        if stdin is None or not self.alive:
            raise MuxClosed("keeper is gone")
        async with self._drain_lock:
            try:
                await stdin.drain()
            except (ConnectionError, RuntimeError) as exc:
                raise MuxClosed(f"keeper pipe closed: {exc}") from exc

    # ── streams ─────────────────────────────────────────────────────────────

    async def open_stream(self, timeout: float | None = 10.0) -> MuxStream:
        if not self.alive:
            raise MuxClosed("keeper is gone")
        sid = self._next_id
        self._next_id = 1 if sid >= _MAX_STREAM_ID else sid + 1
        stream = MuxStream(self, sid)
        self._streams[sid] = stream
        self._send(OPEN, sid)
        try:
            await self._drain()
            await asyncio.wait_for(asyncio.shield(stream.opened), timeout)
        except BaseException:
            await stream.aclose()
            raise
        return stream

    # ── frames in ───────────────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        reason = "keeper exited"
        try:
            while True:
                head = await stdout.readexactly(HEADER.size)
                kind, sid, length = HEADER.unpack(head)
                if length > MAX_PAYLOAD:
                    reason = f"protocol error: {length}-byte frame"
                    break
                payload = await stdout.readexactly(length) if length else b""
                self._dispatch(kind, sid, payload)
        except asyncio.IncompleteReadError:
            pass
        except asyncio.CancelledError:
            reason = "session cancelled"
            raise
        except Exception as exc:  # pragma: no cover - defensive
            reason = f"reader failed: {exc}"
            logger.exception("page keeper %s: frame reader failed", self.label)
        finally:
            self._finish(reason)

    def _dispatch(self, kind: int, sid: int, payload: bytes) -> None:
        if kind == DATA:
            stream = self._streams.get(sid)
            if stream is not None:
                stream._feed(payload)
            else:
                # Late data for a stream we already closed: tell the keeper to
                # drop it, or its pump parks forever on a window nobody will
                # ever reopen and the service socket stays open.
                self._send(RESET, sid)
        elif kind == WINDOW:
            stream = self._streams.get(sid)
            if stream is not None:
                stream._credit(decode_credit(payload))
        elif kind == EOF:
            stream = self._streams.get(sid)
            if stream is not None:
                stream._feed_eof()
        elif kind == OPEN_OK:
            stream = self._streams.get(sid)
            if stream is not None and not stream.opened.done():
                stream.opened.set_result(None)
        elif kind == OPEN_FAIL:
            stream = self._streams.pop(sid, None)
            if stream is not None:
                reason = payload.decode("utf-8", "replace") or "refused"
                stream._fail(ConnectionRefusedError(f"service refused the connection: {reason}"))
        elif kind == RESET:
            stream = self._streams.pop(sid, None)
            if stream is not None:
                stream._fail(StreamReset("reset by the service side"))
        elif kind == HELLO:
            info = _json(payload)
            self.hello = info
            if info.get("version") != PROTOCOL_VERSION:
                logger.warning(
                    "page keeper %s speaks protocol %r, runtime %r",
                    self.label,
                    info.get("version"),
                    PROTOCOL_VERSION,
                )
            if not self._hello.done():
                self._hello.set_result(info)
        elif kind == LISTEN:
            self.listen_scope = str(_json(payload).get("scope") or "") or None
        elif kind == EXIT:
            self.exit_info = _json(payload)

    async def _read_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        while True:
            line = await stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail.append(text)
                logger.debug("page keeper %s: %s", self.label, text)

    def _finish(self, reason: str) -> None:
        if self.closed.done():
            return
        err = MuxClosed(reason)
        for stream in list(self._streams.values()):
            stream._fail(err)
        self._streams.clear()
        if not self._hello.done():
            self._hello.set_exception(err)
            self._hello.exception()
        info = dict(self.exit_info or {})
        info.setdefault("code", None)
        info.setdefault("stopped", False)
        info["reason"] = reason
        self.closed.set_result(info)

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def shutdown(self, grace: float = 15.0) -> dict[str, Any]:
        """Ask the keeper to stop the service, then make sure the process is gone."""
        if self.alive:
            self._send(SHUTDOWN, 0)
            with contextlib.suppress(Exception):
                await self._drain()
        stdin = self.process.stdin
        if stdin is not None:
            with contextlib.suppress(Exception):
                stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=5)
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(self.closed), timeout=5)
        if self._stderr is not None:
            self._stderr.cancel()
        if not self.closed.done():
            self._reader.cancel()
            self._finish("shut down")
        return self.closed.result()


def _json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ── httpcore adapter ─────────────────────────────────────────────────────────


class _HttpcoreStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: MuxStream) -> None:
        self._stream = stream

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            return await self._stream.read(max_bytes, timeout)
        except TimeoutError as exc:
            raise httpcore.ReadTimeout(str(exc) or "read timed out") from exc
        except ConnectionError as exc:
            raise httpcore.ReadError(str(exc)) from exc

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not buffer:
            return
        try:
            await self._stream.write(buffer, timeout)
        except TimeoutError as exc:
            raise httpcore.WriteTimeout(str(exc) or "write timed out") from exc
        except ConnectionError as exc:
            raise httpcore.WriteError(str(exc)) from exc

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("TLS is not used on the page-service transport")

    def get_extra_info(self, info: str) -> Any:
        # httpcore asks "is_readable" before reusing a pooled keep-alive
        # connection: readable-while-idle means the server closed it (or sent
        # garbage), so the pool drops it instead of writing into a dead socket.
        if info == "is_readable":
            return self._stream.readable
        return None


class MuxNetworkBackend(httpcore.AsyncNetworkBackend):
    """Every ``connect_tcp`` opens a stream on one keeper; host/port are ignored."""

    def __init__(self, session: MuxSession) -> None:
        self.session = session

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            stream = await self.session.open_stream(timeout=timeout or 10.0)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("service did not accept the connection") from exc
        except ConnectionError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        return _HttpcoreStream(stream)

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.AsyncNetworkStream:  # pragma: no cover - never configured
        raise httpcore.ConnectError("unix sockets are not used on the page-service transport")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
