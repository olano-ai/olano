"""WhatsApp managed-bridge exit reporting.

A restart tears the bridge down on purpose, and the poll loop outlives that
teardown — ``disconnect()`` sleeps between SIGTERM and SIGKILL and only clears
``_running`` at the very end. Live symptom (agent=andrew): every ordinary
restart logged ``WhatsApp bridge process exited unexpectedly (code 0).`` at
ERROR and pushed the operator a crash notification, for a process we had just
asked to stop.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from olano.platforms.config import PlatformConfig
from olano.platforms.whatsapp import WhatsAppAdapter


@pytest.fixture
def adapter(tmp_path, monkeypatch) -> WhatsAppAdapter:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    return WhatsAppAdapter(PlatformConfig(enabled=True, extra={}), agent_name="andrew")


def _exited(code: int) -> Any:
    proc = MagicMock()
    proc.poll.return_value = code
    return proc


async def test_teardown_exit_is_not_reported_as_a_crash(adapter: WhatsAppAdapter) -> None:
    adapter._bridge_process = _exited(0)
    adapter._shutting_down = True

    assert await adapter._check_managed_bridge_exit() is None
    assert not adapter.has_fatal_error


async def test_unrequested_clean_exit_is_still_surfaced(adapter: WhatsAppAdapter) -> None:
    """Exit 0 outside teardown is a real end-of-session (logged out on the
    phone): worth reporting, but it is not a crash and must not say so."""
    adapter._bridge_process = _exited(0)
    adapter._notify_fatal_error = _noop  # type: ignore[method-assign]

    message = await adapter._check_managed_bridge_exit()

    assert message is not None
    assert "unexpectedly" not in message
    assert "logged out" in message
    assert adapter.has_fatal_error


async def test_nonzero_exit_still_reads_as_a_crash(adapter: WhatsAppAdapter) -> None:
    adapter._bridge_process = _exited(1)
    adapter._notify_fatal_error = _noop  # type: ignore[method-assign]

    message = await adapter._check_managed_bridge_exit()

    assert message is not None
    assert "exited unexpectedly (code 1)" in message


async def test_connect_clears_the_teardown_exemption(adapter: WhatsAppAdapter) -> None:
    """Leaving the flag set after a disconnect would swallow every genuine
    bridge crash for the rest of the process."""
    adapter._shutting_down = True
    adapter._bridge_script = ""  # bail early; we only care about the flag reset

    await adapter.connect()

    assert adapter._shutting_down is False


async def test_disconnect_does_not_wait_on_an_already_dead_bridge(
    adapter: WhatsAppAdapter, monkeypatch
) -> None:
    """The runtime tears every adapter down under one 5s budget, so a fixed
    one-second sleep here is a fifth of it spent on a dead process — that is
    what tips a multi-connector restart into "Stop timed out for agent".
    """
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("olano.platforms.whatsapp.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("olano.platforms.whatsapp.os.killpg", lambda *a: None)
    monkeypatch.setattr("olano.platforms.whatsapp.os.getpgid", lambda _pid: 1234)
    adapter._bridge_process = _exited(0)

    await adapter.disconnect()

    # Polls at 50ms and stops on the first dead reading — never the old flat 1s.
    assert sum(slept) < 1.0
    assert 1 not in slept


# ── Bridge port allocation ───────────────────────────────────────────────────
# Live outage (agent=andrew): bridge_port was saved as 6 — a typo for 3006.
# Ports below 1024 need root, the bridge runs unprivileged, so node died on an
# EACCES bind ~1s into every start and the only visible symptom was "Bridge
# HTTP on port 6 is not responding (Connection refused)". A port nobody can
# bind must not be able to take the connector down.


def test_each_agent_gets_its_own_port_without_anyone_allocating_one() -> None:
    """The whole point of Auto: two agents both running WhatsApp must not
    collide, with no per-agent configuration."""
    a = WhatsAppAdapter.auto_bridge_port("andrew")
    b = WhatsAppAdapter.auto_bridge_port("lucia")

    assert a != b
    assert all(1024 <= p <= 65535 for p in (a, b))
    # Deterministic — a restart must reach the same bridge, not a new port.
    assert WhatsAppAdapter.auto_bridge_port("andrew") == a


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_unset_port_means_auto(configured: Any) -> None:
    assert WhatsAppAdapter.resolve_bridge_port("andrew", configured) == (
        WhatsAppAdapter.auto_bridge_port("andrew")
    )


@pytest.mark.parametrize("configured", [6, "6", 0, -1, 70000, "notaport", 1023])
def test_an_unbindable_port_falls_back_to_auto_instead_of_killing_the_bridge(
    configured: Any, caplog
) -> None:
    with caplog.at_level("WARNING"):
        port = WhatsAppAdapter.resolve_bridge_port("andrew", configured)

    assert port == WhatsAppAdapter.auto_bridge_port("andrew")
    assert "bridge_port" in caplog.text


@pytest.mark.parametrize("configured", [1024, "3006", 65535])
def test_a_usable_explicit_port_is_still_honoured(configured: Any) -> None:
    """Auto is the default, not a straitjacket — a deliberate pin must win."""
    assert WhatsAppAdapter.resolve_bridge_port("andrew", configured) == int(configured)


def test_diagnostics_report_a_stale_unusable_port(tmp_path, monkeypatch) -> None:
    """The runtime is healthy on the auto port, but the config still reads
    wrong — say so rather than letting it silently mislead the next reader."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    adapter = WhatsAppAdapter(
        PlatformConfig(enabled=True, extra={"bridge_port": 6}), agent_name="andrew"
    )

    assert adapter._bridge_port == WhatsAppAdapter.auto_bridge_port("andrew")
    check = next(
        c for c in adapter.diagnostic_snapshot()["checks"] if c["id"] == "bridge_port_override"
    )
    assert check["ok"] is False
    assert "automatic port" in check["message"]


def test_no_override_row_when_the_port_is_fine(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    adapter = WhatsAppAdapter(
        PlatformConfig(enabled=True, extra={"bridge_port": 3006}), agent_name="andrew"
    )

    ids = [c["id"] for c in adapter.diagnostic_snapshot()["checks"]]
    assert "bridge_port_override" not in ids


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None
