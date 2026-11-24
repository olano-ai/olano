"""Regression coverage for headless MCP support.

These guard two bugs found while testing ``olano -n`` with MCP servers:

1. ``_read_piped_input`` blocked forever on a connected-but-idle non-TTY stdin
   (CI runners, supervisors, background shells), hanging every headless run.
2. The output-suppression redirect (``olano.cli._quiet``) used ``io.StringIO``,
   which has no ``fileno()``. stdio MCP servers spawn a subprocess and call
   ``sys.stderr.fileno()`` via ``subprocess.Popen`` → ``io.UnsupportedOperation``.
   A related failure: importing the mcp stdio client *under* that redirect bound
   ``stdio_client``'s ``errlog`` default to a temp file that was later closed,
   so tool calls failed with ``ValueError: I/O operation on closed file``.

The stdio integration test is hermetic: it spawns an in-repo FastMCP server
(``tests/fixtures/mcp_echo_server.py``) over stdio — no network required.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from olano.cli._quiet import run_quietly, run_quietly_async

_ECHO_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


# ── Bug 1: stdin must never block on an idle non-TTY pipe ─────────────────────


def test_read_piped_input_does_not_block_on_idle_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    from olano.cli.main import _read_piped_input

    # A pipe with an open writer but no data is non-TTY and never reaches EOF,
    # so an unconditional read() would block forever.
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "r")
    monkeypatch.setattr("sys.stdin", reader)

    result: dict[str, object] = {}

    def _call() -> None:
        result["value"] = _read_piped_input()

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(timeout=5)
    blocked = worker.is_alive()

    os.close(write_fd)
    reader.close()

    assert not blocked, "_read_piped_input blocked on an idle non-TTY pipe"
    assert result["value"] is None


def test_read_piped_input_returns_payload_when_data_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from olano.cli.main import _read_piped_input

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"piped prompt\n")
    os.close(write_fd)  # writer closed → read() sees data then EOF
    reader = os.fdopen(read_fd, "r")
    monkeypatch.setattr("sys.stdin", reader)

    try:
        assert _read_piped_input() == "piped prompt"
    finally:
        reader.close()


# ── Bug 2: quiet wrappers must expose a real fileno to child processes ────────


def test_run_quietly_preserves_stderr_fileno() -> None:
    def _spawn_child() -> int:
        # Mirrors what the mcp stdio client does: hand sys.stderr to a child.
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "pass"], stderr=sys.stderr)
        proc.wait()
        return sys.stderr.fileno()

    fileno = run_quietly(_spawn_child)
    assert isinstance(fileno, int)


async def test_run_quietly_async_preserves_stderr_fileno() -> None:
    async def _spawn_child() -> int:
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "pass"], stderr=sys.stderr)
        proc.wait()
        return sys.stderr.fileno()

    fileno = await run_quietly_async(_spawn_child)
    assert isinstance(fileno, int)


def test_builder_prewarms_stdio_errlog_with_real_stream() -> None:
    # Importing the builder must bind stdio_client's ``errlog`` default to the
    # real stderr (a stream with a valid fileno), not a redirect/StringIO.
    import mcp.client.stdio as stdio_mod

    import olano.core.builder  # noqa: F401  # triggers the prewarm import

    func = getattr(stdio_mod.stdio_client, "__wrapped__", stdio_mod.stdio_client)
    defaults = func.__defaults__ or ()
    assert defaults, "stdio_client should expose an errlog default"
    errlog = defaults[0]
    # Must be a live, fileno-capable stream (would raise if it were a StringIO
    # or a closed temp file captured under a redirect).
    assert isinstance(errlog.fileno(), int)


# ── Full stdio MCP round-trip through the quiet build (hermetic) ──────────────


async def test_stdio_mcp_tools_load_and_invoke_after_quiet_build(base_config) -> None:
    from olano.config.agent_config import MCPServerConfig
    from olano.core.builder import _build_mcp_tools

    base_config.mcp_servers = [
        MCPServerConfig(
            url="echo",
            transport="stdio",
            command=sys.executable,
            args=[str(_ECHO_SERVER)],
        )
    ]

    # Build the MCP tools *under* the output-suppression redirect — this is the
    # exact path that originally crashed with `fileno` / `closed file`.
    tools, _summary = await run_quietly_async(_build_mcp_tools, base_config)
    by_name = {tool.name: tool for tool in tools}
    assert "echo_text" in by_name, f"expected echo_text, got {sorted(by_name)}"

    # Invoke *after* the redirect has closed: the stdio session is created
    # lazily here, so a stale/closed errlog would surface as a ValueError.
    result = await by_name["echo_text"].ainvoke({"text": "ping"})
    assert "ping" in str(result)


async def test_disabled_stdio_mcp_server_is_skipped(base_config) -> None:
    from olano.config.agent_config import MCPServerConfig
    from olano.core.builder import _build_mcp_tools

    base_config.mcp_servers = [
        MCPServerConfig(
            url="echo",
            transport="stdio",
            enabled=False,
            command=sys.executable,
            args=[str(_ECHO_SERVER)],
        )
    ]

    tools, summary = await _build_mcp_tools(base_config)
    assert tools == []
    assert [s["status"] for s in summary] == ["disabled"]


async def test_disabled_mcp_tools_are_filtered(base_config) -> None:
    from olano.config.agent_config import MCPServerConfig
    from olano.core.builder import _build_mcp_tools

    base_config.mcp_servers = [
        MCPServerConfig(
            url="echo",
            transport="stdio",
            command=sys.executable,
            args=[str(_ECHO_SERVER)],
            disabled_tools=["echo_text"],
        )
    ]

    tools, summary = await _build_mcp_tools(base_config)
    assert tools == []
    # The server still connects (status connected) — only its tool is filtered.
    assert summary and summary[0]["status"] == "connected"
    assert summary[0]["tools"] == []


async def test_disabled_tools_do_not_leak_across_servers(base_config) -> None:
    """A deny-list belongs to the server that declares it.

    The bound tool list used to be filtered against the *union* of every
    server's ``disabled_tools``, so turning one name off on server A also
    stripped the same-named tool from server B. Disabling a big server's whole
    catalogue (the dashboard's "Disable all") would have emptied its siblings.
    """
    from olano.config.agent_config import MCPServerConfig
    from olano.core.builder import _build_mcp_tools

    base_config.mcp_servers = [
        MCPServerConfig(
            url="echo-muted",
            transport="stdio",
            command=sys.executable,
            args=[str(_ECHO_SERVER)],
            disabled_tools=["echo_text"],
        ),
        MCPServerConfig(
            url="echo-live",
            transport="stdio",
            command=sys.executable,
            args=[str(_ECHO_SERVER)],
        ),
    ]

    tools, summary = await _build_mcp_tools(base_config)

    assert [getattr(t, "name", "") for t in tools] == ["echo_text"]
    by_url = {row["url"]: row for row in summary}
    assert by_url["echo-muted"]["tools"] == []
    assert by_url["echo-live"]["tools"] == ["echo_text"]


# ── concurrent boot connects ─────────────────────────────────────────────────
#
# Boot used to await each server in turn, so every BOOT_CONNECT_TIMEOUT was
# additive — an agent with two unreachable servers burned 60s of boot before it
# could start. The connects now overlap; these guard the two properties that
# change made load-bearing: they really do overlap, and the reported order no
# longer depends on which server happens to answer first.


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def _fake_mcp_client(monkeypatch, behaviour: dict[str, float | Exception]):
    """Patch MultiServerMCPClient so each server sleeps (or raises) on connect.

    ``_build_mcp_tools`` imports the class from ``langchain_mcp_adapters.client``
    at call time, so patching the module attribute is enough.
    """
    import langchain_mcp_adapters.client as client_mod

    class _FakeClient:
        def __init__(self, conf: dict) -> None:
            self._url = next(iter(conf))

        async def get_tools(self):
            outcome = behaviour[self._url]
            if isinstance(outcome, Exception):
                raise outcome
            await asyncio.sleep(outcome)
            return [_FakeTool(f"{self._url}_tool")]

    monkeypatch.setattr(client_mod, "MultiServerMCPClient", _FakeClient)


def _stdio_server(url: str):
    from olano.config.agent_config import MCPServerConfig

    return MCPServerConfig(url=url, transport="stdio", command=sys.executable, args=["-c", "pass"])


async def test_mcp_boot_connects_overlap_and_keep_configured_order(
    base_config, monkeypatch
) -> None:
    from olano.core.builder import _build_mcp_tools

    # Deliberately inverted: the first-configured server finishes last.
    _fake_mcp_client(monkeypatch, {"slow": 0.30, "mid": 0.20, "fast": 0.05})
    base_config.mcp_servers = [_stdio_server(u) for u in ("slow", "mid", "fast")]

    started = time.perf_counter()
    tools, summary = await _build_mcp_tools(base_config)
    elapsed = time.perf_counter() - started

    # Serial would cost ~0.55s; concurrent is bounded by the slowest (~0.30s).
    assert elapsed < 0.50, f"connects did not overlap (took {elapsed:.2f}s)"
    # Finish order was fast → mid → slow; reported order must stay configured.
    assert [s["url"] for s in summary] == ["slow", "mid", "fast"]
    assert [t.name for t in tools] == ["slow_tool", "mid_tool", "fast_tool"]


async def test_mcp_boot_order_holds_across_skipped_and_failed_servers(
    base_config, monkeypatch
) -> None:
    """A server resolved without connecting (disabled) and one that raises must
    still land at their configured index, not be appended at the end."""
    from olano.core.builder import _build_mcp_tools

    _fake_mcp_client(monkeypatch, {"ok": 0.05, "boom": RuntimeError("handshake failed")})
    servers = [_stdio_server("ok"), _stdio_server("off"), _stdio_server("boom")]
    servers[1].enabled = False
    base_config.mcp_servers = servers

    tools, summary = await run_quietly_async(_build_mcp_tools, base_config)

    assert [s["url"] for s in summary] == ["ok", "off", "boom"]
    assert [s["status"] for s in summary] == ["connected", "disabled", "error"]
    # One bad server must not cost its siblings their tools.
    assert [t.name for t in tools] == ["ok_tool"]


async def test_mcp_server_needing_consent_is_auth_required_not_error(
    base_config, monkeypatch
) -> None:
    """A connect that ends at the non-interactive OAuth fuse is `auth_required`.

    The distinction is not cosmetic. `error` is in mcp_health's
    RECOVERABLE_STATUSES, so filing this there put the server on the sweep's
    reconnect backoff — rebuilding the agent's whole graph every 5 minutes,
    then every 6 hours, forever, to re-ask a consent question that only a human
    at a browser can answer. `auth_required` with no usable token is the one
    status the sweep is documented never to retry.
    """
    from olano.core.builder import _build_mcp_tools
    from olano.core.mcp_setup import McpAuthorizationRequired

    # anyio's task group wraps the connect, so the fuse arrives as a group —
    # exactly how it reaches the boot path in production.
    wrapped = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [McpAuthorizationRequired("https://mcp.stripe.com")]
    )
    _fake_mcp_client(monkeypatch, {"ok": 0.01, "needs-auth": wrapped})
    base_config.mcp_servers = [_stdio_server("ok"), _stdio_server("needs-auth")]

    tools, summary = await run_quietly_async(_build_mcp_tools, base_config)

    assert [s["status"] for s in summary] == ["connected", "auth_required"]
    assert summary[1]["message"] == "OAuth authorization required"
    assert [t.name for t in tools] == ["ok_tool"]


async def test_mcp_server_that_merely_broke_stays_an_error(base_config, monkeypatch) -> None:
    """The carve-out above must stay narrow: a 401 from a gateway, a mis-scoped
    token, a server that fell over — all still `error`, all still retried. Only
    the fuse itself means "nothing but a human can move this forward"."""
    from olano.core.builder import _build_mcp_tools

    _fake_mcp_client(monkeypatch, {"boom": RuntimeError("401 Unauthorized from the edge")})
    base_config.mcp_servers = [_stdio_server("boom")]

    _tools, summary = await run_quietly_async(_build_mcp_tools, base_config)

    assert summary[0]["status"] == "error"


async def test_build_mcp_tools_empty_without_servers(base_config) -> None:
    from olano.core.builder import _build_mcp_tools

    base_config.mcp_servers = []
    tools, summary = await _build_mcp_tools(base_config)
    assert tools == []
    assert summary == []


async def test_boot_abandons_connect_that_ignores_cancellation(base_config, monkeypatch) -> None:
    """A hung MCP connect whose cancellation also wedges must not block the boot.

    Regression: a stdio server wrapping ``mcp-remote`` sits in an interactive
    OAuth prompt forever on a headless host; cancelling the connect can wedge
    inside the SDK's cleanup, and ``asyncio.wait_for`` would then never return —
    the runtime stayed down until killed. The boot path must give up after
    BOOT_CONNECT_TIMEOUT + BOOT_CANCEL_GRACE and abandon the task.
    """
    import asyncio

    import langchain_mcp_adapters.client as adapters_client

    from olano.config.agent_config import MCPServerConfig
    from olano.core import mcp_setup
    from olano.core.builder import _build_mcp_tools

    class _HungClient:
        def __init__(self, connections):  # signature match for MultiServerMCPClient
            pass

        async def get_tools(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Wedged transport cleanup: swallow the first cancellation
                # (this is what makes plain wait_for hang forever). Honor the
                # next one so the test can reap the abandoned task.
                await asyncio.sleep(3600)
            return []

    monkeypatch.setattr(adapters_client, "MultiServerMCPClient", _HungClient)
    monkeypatch.setattr(mcp_setup, "BOOT_CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(mcp_setup, "BOOT_CANCEL_GRACE", 0.05)

    base_config.mcp_servers = [MCPServerConfig(url="http://127.0.0.1:59999/mcp", transport="http")]

    before = asyncio.all_tasks()
    start = asyncio.get_event_loop().time()
    tools, summary = await _build_mcp_tools(base_config)
    elapsed = asyncio.get_event_loop().time() - start

    assert tools == []
    assert [s["status"] for s in summary] == ["timeout"]
    assert elapsed < 5.0, f"boot blocked for {elapsed:.1f}s on a wedged MCP connect"

    # Reap the deliberately-abandoned task so it can't leak past this test.
    leftovers = [t for t in asyncio.all_tasks() - before if not t.done()]
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)
