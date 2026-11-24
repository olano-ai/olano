"""Tests for the SignalAdapter managed signal-cli daemon supervision.

Covers the opt-in + attach-if-running behavior added so olano can spawn,
health-check, crash-detect, and tear down `signal-cli daemon --http` itself
(mirroring the WhatsApp bridge), without breaking the original "attach to an
externally-run daemon" path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from olano.platforms.config import PlatformConfig
from olano.platforms.signal import SignalAdapter


def _adapter(**extra) -> SignalAdapter:
    base = {"http_url": "http://127.0.0.1:9000", "account": "+15551230000"}
    base.update(extra)
    cfg = PlatformConfig(enabled=True, extra=base)
    adapter = SignalAdapter(cfg, agent_name="test-signal")
    # Avoid touching the real lock files; assert on the release instead.
    adapter._acquire_platform_lock = MagicMock(return_value=True)  # type: ignore[method-assign]
    adapter._release_platform_lock = MagicMock()  # type: ignore[method-assign]
    # Keep the background loops inert so connect() doesn't do real network I/O.
    adapter._sse_listener = AsyncMock()  # type: ignore[method-assign]
    adapter._health_monitor = AsyncMock()  # type: ignore[method-assign]
    return adapter


def _fake_proc(poll_value):
    proc = MagicMock()
    proc.pid = 4321
    proc.poll.return_value = poll_value
    return proc


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


async def test_manage_daemon_false_unreachable_preserves_behavior():
    """Default (manage_daemon=false) + unreachable daemon → False, no spawn."""
    adapter = _adapter()  # no manage_daemon
    adapter._health_check_once = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with patch("olano.platforms.signal.subprocess.Popen") as popen:
        ok = await adapter.connect()

    assert ok is False
    popen.assert_not_called()
    adapter._release_platform_lock.assert_called_once()
    assert adapter.client is None


async def test_attach_if_running_skips_spawn():
    """manage_daemon=true but a daemon is already reachable → attach, no spawn."""
    adapter = _adapter(manage_daemon=True)
    adapter._health_check_once = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with patch("olano.platforms.signal.subprocess.Popen") as popen:
        ok = await adapter.connect()

    assert ok is True
    popen.assert_not_called()
    assert adapter._daemon_managed_by_us is False
    assert adapter._running is True
    await adapter.disconnect()


async def test_spawn_when_unreachable():
    """manage_daemon=true + unreachable → spawn signal-cli daemon and attach."""
    adapter = _adapter(manage_daemon=True)
    # First probe (in connect) False; readiness probe (in _spawn_daemon) True.
    adapter._health_check_once = AsyncMock(side_effect=[False, True])  # type: ignore[method-assign]

    with (
        patch("olano.platforms.signal.subprocess.Popen", return_value=_fake_proc(None)) as popen,
        patch("olano.platforms.signal.shutil.which", return_value="/usr/bin/signal-cli"),
        patch("olano.platforms.signal._kill_port_process"),
        patch("olano.platforms.signal.asyncio.sleep", new=AsyncMock()),
    ):
        ok = await adapter.connect()

    assert ok is True
    assert adapter._daemon_managed_by_us is True
    popen.assert_called_once()
    argv = popen.call_args.args[0]
    # Account-scoped daemon bound to our host:port.
    assert "-a" in argv and "+15551230000" in argv
    assert "daemon" in argv and "--http" in argv
    assert "127.0.0.1:9000" in argv
    # Process-group isolation requested on POSIX.
    assert "preexec_fn" in popen.call_args.kwargs


async def test_spawn_failure_sets_fatal_and_cleans_up():
    """Daemon exits during startup → fatal error, lock released, client closed."""
    adapter = _adapter(manage_daemon=True)
    adapter._health_check_once = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with (
        patch("olano.platforms.signal.subprocess.Popen", return_value=_fake_proc(1)),
        patch("olano.platforms.signal.shutil.which", return_value="/usr/bin/signal-cli"),
        patch("olano.platforms.signal._kill_port_process"),
        patch("olano.platforms.signal.asyncio.sleep", new=AsyncMock()),
    ):
        ok = await adapter.connect()

    assert ok is False
    assert adapter.has_fatal_error
    adapter._release_platform_lock.assert_called_once()
    assert adapter.client is None


async def test_signal_cli_missing_sets_fatal():
    """signal-cli not found → non-retryable fatal, no Popen."""
    adapter = _adapter(manage_daemon=True, signal_cli_path="nope-signal-cli")
    adapter._health_check_once = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with (
        patch("olano.platforms.signal.shutil.which", return_value=None),
        patch("olano.platforms.signal.subprocess.Popen") as popen,
    ):
        ok = await adapter.connect()

    assert ok is False
    assert adapter.has_fatal_error
    popen.assert_not_called()


# ---------------------------------------------------------------------------
# disconnect() / teardown
# ---------------------------------------------------------------------------


async def test_disconnect_kills_managed_daemon():
    """A daemon we spawned is killed (process group SIGTERM) on disconnect."""
    adapter = _adapter(manage_daemon=True)
    adapter._daemon_process = _fake_proc(0)  # poll()==0 → dead after SIGTERM
    adapter._daemon_managed_by_us = True
    adapter._daemon_log_fh = MagicMock()

    with (
        patch("olano.platforms.signal.os.getpgid", return_value=999),
        patch("olano.platforms.signal.os.killpg") as killpg,
        patch("olano.platforms.signal.asyncio.sleep", new=AsyncMock()),
    ):
        await adapter.disconnect()

    killpg.assert_called()  # SIGTERM issued to the process group
    assert adapter._daemon_process is None
    adapter._release_platform_lock.assert_called_once()


async def test_disconnect_leaves_external_daemon():
    """An attached (external) daemon is left running on disconnect."""
    adapter = _adapter(manage_daemon=True)
    adapter._daemon_process = None
    adapter._daemon_managed_by_us = False

    with patch("olano.platforms.signal.os.killpg") as killpg:
        await adapter.disconnect()

    killpg.assert_not_called()
    adapter._release_platform_lock.assert_called_once()


# ---------------------------------------------------------------------------
# crash detection + config parsing
# ---------------------------------------------------------------------------


async def test_check_managed_daemon_exit_surfaces_fatal():
    """A crashed supervised daemon is detected and surfaced as a fatal error."""
    adapter = _adapter(manage_daemon=True)
    adapter._daemon_process = _fake_proc(137)  # killed
    adapter._daemon_managed_by_us = True
    adapter._daemon_log = "/tmp/daemon.log"
    adapter._notify_fatal_error = AsyncMock()  # type: ignore[method-assign]

    msg = await adapter._check_managed_daemon_exit()

    assert msg and "137" in msg
    assert adapter.has_fatal_error
    adapter._notify_fatal_error.assert_awaited_once()


async def test_check_managed_daemon_exit_ignores_external():
    """An external daemon (not managed by us) is never crash-flagged."""
    adapter = _adapter(manage_daemon=True)
    adapter._daemon_process = _fake_proc(1)
    adapter._daemon_managed_by_us = False

    assert await adapter._check_managed_daemon_exit() is None
    assert not adapter.has_fatal_error


def test_daemon_host_port_parsed_from_http_url():
    assert _adapter()._daemon_port == 9000
    assert _adapter(http_url="http://127.0.0.1:8080")._daemon_port == 8080
    assert _adapter(http_url="http://0.0.0.0:7000")._daemon_host == "0.0.0.0"
