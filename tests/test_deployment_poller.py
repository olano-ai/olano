"""SettingsPoller — refresh loop, transition handlers, SSE broadcast."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.deployment import resolver
from olano.deployment.poller import SettingsPoller
from olano.deployment.resolver import DeploymentStatus
from olano.deployment.schema import DeploymentSettings


def status(posture="enforced", revision=1, features=None, **kw) -> DeploymentStatus:
    return DeploymentStatus(
        posture=posture,
        source=kw.pop("source", "cloud"),
        settings=DeploymentSettings(features=features or {}),
        policy_revision=revision,
        **kw,
    )


@pytest.fixture
def push_event() -> MagicMock:
    return MagicMock()


async def run_transition(poller: SettingsPoller, new_status: DeploymentStatus) -> None:
    await poller._apply_transitions(new_status)


class TestFingerprint:
    def test_same_status_same_fingerprint(self):
        a = status(revision=3)
        b = status(revision=3)
        assert SettingsPoller._fingerprint(a) == SettingsPoller._fingerprint(b)

    def test_settings_change_changes_fingerprint(self):
        a = status(revision=3)
        b = status(revision=3, features={"terminal": False})
        assert SettingsPoller._fingerprint(a) != SettingsPoller._fingerprint(b)

    def test_revision_change_changes_fingerprint(self):
        assert SettingsPoller._fingerprint(status(revision=3)) != SettingsPoller._fingerprint(
            status(revision=4)
        )


class TestTransitions:
    async def test_broadcasts_sse_event(self, push_event, monkeypatch):
        poller = SettingsPoller(push_event=push_event)
        await run_transition(poller, status(revision=7))
        push_event.assert_called_once()
        event_type, payload = push_event.call_args[0]
        assert event_type == "deployment_settings_changed"
        assert payload["policy_revision"] == 7

    async def test_terminal_disable_closes_sessions(self, push_event, monkeypatch):
        close_all = AsyncMock(return_value=2)
        monkeypatch.setattr("olano.runtime.terminal.close_all_sessions", close_all)
        poller = SettingsPoller(push_event=push_event)
        await run_transition(poller, status(features={"terminal": False}))
        close_all.assert_awaited_once()

    async def test_shell_disable_also_closes_sessions(self, push_event, monkeypatch):
        close_all = AsyncMock(return_value=0)
        monkeypatch.setattr("olano.runtime.terminal.close_all_sessions", close_all)
        poller = SettingsPoller(push_event=push_event)
        await run_transition(poller, status(features={"shell": False}))
        close_all.assert_awaited_once()

    async def test_terminal_enabled_does_not_close(self, push_event, monkeypatch):
        close_all = AsyncMock()
        monkeypatch.setattr("olano.runtime.terminal.close_all_sessions", close_all)
        poller = SettingsPoller(push_event=push_event)
        await run_transition(poller, status())
        close_all.assert_not_awaited()

    async def test_transition_errors_never_raise(self, monkeypatch):
        boom = MagicMock(side_effect=RuntimeError("boom"))
        poller = SettingsPoller(push_event=boom)
        await run_transition(poller, status())  # must not raise
        boom.assert_called_once()


class TestLoop:
    async def test_loop_refreshes_and_fires_on_change(self, push_event, monkeypatch):
        statuses = [status(revision=1), status(revision=2), status(revision=2)]
        calls = 0

        async def fake_refresh(**kw):
            nonlocal calls
            result = statuses[min(calls, len(statuses) - 1)]
            calls += 1
            return result

        monkeypatch.setattr(resolver, "refresh_async", fake_refresh)
        monkeypatch.setattr(resolver, "deployment_status", lambda now=None: statuses[min(calls, 2)])
        poller = SettingsPoller(push_event=push_event)
        poller._last_fingerprint = SettingsPoller._fingerprint(statuses[0])
        # Shrink the sleep so the loop iterates fast.
        monkeypatch.setattr(poller, "_next_interval", lambda st: 0.01)

        task = asyncio.create_task(poller._loop())
        try:
            for _ in range(200):
                if calls >= 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls >= 3
        # revision 1→2 changed once; the second rev-2 poll must not re-fire.
        assert push_event.call_count == 1

    async def test_start_stop(self, monkeypatch):
        monkeypatch.setattr(resolver, "deployment_status", lambda now=None: status(revision=1))

        async def fake_refresh(**kw):
            return status(revision=1)

        monkeypatch.setattr(resolver, "refresh_async", fake_refresh)
        poller = SettingsPoller()
        poller.start()
        assert poller._task is not None
        await asyncio.sleep(0)
        poller.stop()
        assert poller._task is None


class TestSettledHook:
    """The first-boot retry rides here: it fires once the box is OUT of
    quarantine, so a step the boot-time 'no artifact yet' posture refused
    runs again when the settings land — not at a next boot that never comes."""

    async def test_runs_when_a_change_lands_outside_quarantine(self, push_event):
        hook = AsyncMock()
        poller = SettingsPoller(push_event=push_event, on_settled=hook)
        await run_transition(poller, status())
        hook.assert_awaited_once()

    async def test_stays_silent_while_still_quarantined(self, push_event):
        hook = AsyncMock()
        poller = SettingsPoller(push_event=push_event, on_settled=hook)
        await run_transition(poller, status(posture="quarantine", source="quarantine"))
        hook.assert_not_awaited()

    async def test_hook_errors_never_raise_and_never_block_the_broadcast(self, push_event):
        hook = AsyncMock(side_effect=RuntimeError("boom"))
        poller = SettingsPoller(push_event=push_event, on_settled=hook)
        await run_transition(poller, status())
        hook.assert_awaited_once()
        push_event.assert_called_once()

    async def test_no_hook_is_fine(self, push_event):
        poller = SettingsPoller(push_event=push_event)
        await run_transition(poller, status())
        push_event.assert_called_once()
