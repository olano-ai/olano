"""Tests for olano.platform_control (detection, controller, confirm store)
and the mode-aware update strategies."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from olano.platform_control import controller as ctrl
from olano.platform_control.controller import Notify, PlatformController
from olano.platform_control.detect import (
    InstallMode,
    SupervisionMode,
    detect_install_mode,
    detect_supervision,
    systemd_unit_name,
)

# ─────────────────────────────── detection ──────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Neutralize every deployment signal so each test opts in explicitly."""
    for var in (
        "LIONCLAW_CONTAINERIZED",
        "INVOCATION_ID",
        "LIONCLAW_DAEMON",
        "LIONCLAW_INSTALL_MODE",
        "LIONCLAW_SYSTEMD_UNIT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("olano.security.sharing.is_running_in_container", lambda: False)
    monkeypatch.setattr("olano.runtime.daemon.read_pid", lambda: None)
    return monkeypatch


class TestDetectSupervision:
    def test_docker_via_env(self, clean_env) -> None:
        clean_env.setattr("olano.security.sharing.is_running_in_container", lambda: True)
        assert detect_supervision() is SupervisionMode.DOCKER

    def test_systemd_via_invocation_id(self, clean_env) -> None:
        clean_env.setenv("INVOCATION_ID", "abc123")
        assert detect_supervision() is SupervisionMode.SYSTEMD

    def test_daemon_via_env(self, clean_env) -> None:
        clean_env.setenv("LIONCLAW_DAEMON", "1")
        assert detect_supervision() is SupervisionMode.DAEMON

    def test_daemon_via_pidfile(self, clean_env) -> None:
        clean_env.setattr("olano.runtime.daemon.read_pid", lambda: os.getpid())
        assert detect_supervision() is SupervisionMode.DAEMON

    def test_foreground_default(self, clean_env) -> None:
        assert detect_supervision() is SupervisionMode.FOREGROUND

    def test_docker_wins_over_systemd(self, clean_env) -> None:
        clean_env.setattr("olano.security.sharing.is_running_in_container", lambda: True)
        clean_env.setenv("INVOCATION_ID", "abc123")
        assert detect_supervision() is SupervisionMode.DOCKER


class TestDetectInstallMode:
    def test_env_override_wins(self, clean_env) -> None:
        clean_env.setenv("LIONCLAW_INSTALL_MODE", "docker")
        assert detect_install_mode() is InstallMode.DOCKER
        clean_env.setenv("LIONCLAW_INSTALL_MODE", "local")
        assert detect_install_mode() is InstallMode.UV_TOOL
        clean_env.setenv("LIONCLAW_INSTALL_MODE", "source")
        assert detect_install_mode() is InstallMode.SOURCE

    def test_frozen_binary(self, clean_env) -> None:
        clean_env.setattr(sys, "frozen", True, raising=False)
        assert detect_install_mode() is InstallMode.BINARY

    def test_git_checkout_is_source(self, clean_env) -> None:
        # The test suite itself runs from a git checkout.
        assert detect_install_mode() is InstallMode.SOURCE

    def test_container_without_git_is_docker(self, clean_env) -> None:
        clean_env.setattr("olano.platform_control.detect.repo_root", lambda: None)
        clean_env.setattr("olano.security.sharing.is_running_in_container", lambda: True)
        assert detect_install_mode() is InstallMode.DOCKER

    def test_install_state_mode(self, clean_env, tmp_path: Path) -> None:
        from olano.update.install_state import InstallState, write_install_state

        clean_env.setattr("olano.platform_control.detect.repo_root", lambda: None)
        write_install_state(tmp_path, InstallState(version="1.0.0", mode="docker"))
        assert detect_install_mode(tmp_path) is InstallMode.DOCKER

    def test_fallback_uv_tool(self, clean_env, tmp_path: Path) -> None:
        clean_env.setattr("olano.platform_control.detect.repo_root", lambda: None)
        assert detect_install_mode(tmp_path) is InstallMode.UV_TOOL


class TestSystemdUnitName:
    def test_env_override(self, clean_env) -> None:
        clean_env.setenv("LIONCLAW_SYSTEMD_UNIT", "olano.service")
        assert systemd_unit_name() == "olano.service"

    def test_no_signal_returns_none_or_service(self, clean_env) -> None:
        # Depending on the host cgroup this is None or a *.service name —
        # it must never raise.
        unit = systemd_unit_name()
        assert unit is None or unit.endswith(".service")


# ─────────────────────────────── controller ─────────────────────────────────


def _fake_runtime() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        _shutdown_event=asyncio.Event(),
        _post_stop_action=None,
        _runners={"agentfather": object()},
    )


@pytest.fixture
def fast_actions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Instant action delay + marker redirected into tmp_path."""
    monkeypatch.setenv("LIONCLAW_PLATFORM_ACTION_DELAY", "0.01")
    monkeypatch.setattr(ctrl, "_marker_path", lambda: tmp_path / "platform_action.json")
    return tmp_path


class TestScheduleRestart:
    async def test_supervised_sets_event_only(self, monkeypatch, fast_actions) -> None:
        runtime = _fake_runtime()
        monkeypatch.setattr(ctrl, "_RUNTIME", runtime)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.SYSTEMD)
        controller = PlatformController()
        strategy = controller.schedule_restart(notify=Notify("a", "telegram", "42"))
        assert strategy == "supervised-exit"
        await asyncio.wait_for(runtime._shutdown_event.wait(), timeout=2)
        assert runtime._post_stop_action is None

    async def test_unsupervised_sets_execv_post_action(self, monkeypatch, fast_actions) -> None:
        runtime = _fake_runtime()
        monkeypatch.setattr(ctrl, "_RUNTIME", runtime)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.DAEMON)
        controller = PlatformController()
        assert controller.schedule_restart() == "execv"
        await asyncio.wait_for(runtime._shutdown_event.wait(), timeout=2)
        assert runtime._post_stop_action is ctrl._execv_self

    async def test_marker_written_with_notify(self, monkeypatch, fast_actions) -> None:
        runtime = _fake_runtime()
        monkeypatch.setattr(ctrl, "_RUNTIME", runtime)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.SYSTEMD)
        PlatformController().schedule_restart(notify=Notify("boss", "telegram", "chat-1"))
        await asyncio.wait_for(runtime._shutdown_event.wait(), timeout=2)
        raw = json.loads((fast_actions / "platform_action.json").read_text())
        assert raw["action"] == "restart"
        assert raw["notify"] == {"agent": "boss", "channel": "telegram", "target": "chat-1"}


class TestScheduleShutdown:
    async def test_docker_unsupported(self, monkeypatch, fast_actions) -> None:
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.DOCKER)
        assert PlatformController().schedule_shutdown() == "unsupported"

    async def test_daemon_exits_cleanly(self, monkeypatch, fast_actions) -> None:
        runtime = _fake_runtime()
        monkeypatch.setattr(ctrl, "_RUNTIME", runtime)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.DAEMON)
        assert PlatformController().schedule_shutdown() == "exit"
        await asyncio.wait_for(runtime._shutdown_event.wait(), timeout=2)
        assert runtime._post_stop_action is None


class TestUpdateGuard:
    async def test_single_flight(self, monkeypatch) -> None:
        controller = PlatformController()
        release = asyncio.Event()

        async def _slow_update(notify, **kwargs) -> None:
            await release.wait()

        monkeypatch.setattr(controller, "_run_update", _slow_update)
        assert controller.begin_update() is True
        assert controller.update_in_progress() is True
        assert controller.begin_update() is False
        release.set()
        await asyncio.wait_for(controller._update_task, timeout=2)
        assert controller.update_in_progress() is False


class TestMutualExclusion:
    async def test_restart_refused_while_updating(self, monkeypatch, fast_actions) -> None:
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.SYSTEMD)
        controller = PlatformController()
        release = asyncio.Event()

        async def _slow_update(notify, **kwargs) -> None:
            await release.wait()

        monkeypatch.setattr(controller, "_run_update", _slow_update)
        controller.begin_update()
        assert controller.schedule_restart() == "busy-update"
        assert controller.schedule_shutdown() == "busy-update"
        release.set()
        await asyncio.wait_for(controller._update_task, timeout=2)

    async def test_update_refused_while_action_pending(self, monkeypatch, fast_actions) -> None:
        runtime = _fake_runtime()
        monkeypatch.setattr(ctrl, "_RUNTIME", runtime)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.SYSTEMD)
        controller = PlatformController()
        controller.schedule_restart()  # schedules a pending _action_task
        assert controller.action_pending() is True
        assert controller.begin_update() is False
        assert controller.schedule_restart() == "busy-action"
        await asyncio.wait_for(runtime._shutdown_event.wait(), timeout=2)

    async def test_post_update_restart_bypasses_guard(self, monkeypatch, fast_actions) -> None:
        # _run_update restarts even though its own update task is "in progress".
        runtime = _fake_runtime()
        monkeypatch.setattr(ctrl, "_RUNTIME", runtime)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.SYSTEMD)
        from olano.update.strategies import UpdateApply

        monkeypatch.setattr(
            "olano.update.strategies.apply_update",
            lambda home, **kwargs: UpdateApply(True, "log", "Updated.", needs_restart=True),
        )
        monkeypatch.setattr(ctrl, "send_followup", lambda notify, text: _true())
        controller = PlatformController()
        controller.begin_update()
        await asyncio.wait_for(controller._update_task, timeout=3)
        # the post-update restart fired despite update_in_progress being true mid-run
        await asyncio.wait_for(runtime._shutdown_event.wait(), timeout=2)


async def _true() -> bool:
    return True


class TestExitWatchdog:
    """A scheduled stop must end in a process exit even if teardown wedges."""

    async def test_armed_on_delayed_stop_and_fires(self, monkeypatch, fast_actions) -> None:
        import threading

        fired: list[object] = []
        done = threading.Event()

        def _fake_force_exit(post_action) -> None:
            fired.append(post_action)
            done.set()

        monkeypatch.setenv("LIONCLAW_EXIT_WATCHDOG_TIMEOUT", "0.05")
        monkeypatch.setattr(ctrl, "_force_exit", _fake_force_exit)
        runtime = _fake_runtime()
        monkeypatch.setattr(ctrl, "_RUNTIME", runtime)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.SYSTEMD)
        assert PlatformController().schedule_restart() == "supervised-exit"
        await asyncio.wait_for(runtime._shutdown_event.wait(), timeout=2)
        # The process (here: the test) didn't exit within the deadline → the
        # watchdog daemon thread must call the force-exit path.
        assert await asyncio.to_thread(done.wait, 2)
        assert fired == [None]  # supervised exit carries no post-action

    async def test_carries_execv_post_action_when_unsupervised(
        self, monkeypatch, fast_actions
    ) -> None:
        import threading

        fired: list[object] = []
        done = threading.Event()
        monkeypatch.setenv("LIONCLAW_EXIT_WATCHDOG_TIMEOUT", "0.05")
        monkeypatch.setattr(
            ctrl, "_force_exit", lambda post_action: (fired.append(post_action), done.set())
        )
        runtime = _fake_runtime()
        monkeypatch.setattr(ctrl, "_RUNTIME", runtime)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.DAEMON)
        assert PlatformController().schedule_restart() == "execv"
        await asyncio.wait_for(runtime._shutdown_event.wait(), timeout=2)
        assert await asyncio.to_thread(done.wait, 2)
        assert fired == [ctrl._execv_self]

    def test_disabled_by_zero_timeout(self, monkeypatch) -> None:
        import threading

        monkeypatch.setenv("LIONCLAW_EXIT_WATCHDOG_TIMEOUT", "0")
        ctrl._arm_exit_watchdog(None)
        assert not any(t.name == "olano-exit-watchdog" for t in threading.enumerate())

    def test_force_exit_runs_post_action_then_exits(self, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(ctrl.os, "_exit", lambda code: calls.append(f"exit:{code}"))
        # A post-action that *returns* means the execv didn't take — the
        # watchdog must still leave the process.
        ctrl._force_exit(lambda: calls.append("post"))
        assert calls == ["post", "exit:1"]

    def test_force_exit_survives_post_action_error(self, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(ctrl.os, "_exit", lambda code: calls.append(f"exit:{code}"))
        ctrl._force_exit(MagicMock(side_effect=RuntimeError("boom")))
        assert calls == ["exit:1"]


class TestCanControl:
    def test_no_runtime(self, monkeypatch) -> None:
        monkeypatch.setattr(ctrl, "_RUNTIME", None)
        assert PlatformController().can_control() is False

    def test_with_runtime(self, monkeypatch) -> None:
        monkeypatch.setattr(ctrl, "_RUNTIME", _fake_runtime())
        assert PlatformController().can_control() is True

    async def test_no_runtime_stop_does_not_exit(self, monkeypatch, fast_actions) -> None:
        # Defensive: _delayed_stop must never os._exit when no runtime is bound.
        monkeypatch.setattr(ctrl, "_RUNTIME", None)
        monkeypatch.setattr(ctrl, "detect_supervision", lambda: SupervisionMode.DAEMON)
        controller = PlatformController()
        controller.schedule_restart()
        # If this awaited task called os._exit, the test process would die.
        await asyncio.wait_for(controller._action_task, timeout=2)


def _write_restart_marker(tmp_path: Path, *, ts: float, notify: dict | None) -> Path:
    marker = tmp_path / "platform_action.json"
    marker.write_text(json.dumps({"action": "restart", "ts": ts, "notify": notify}))
    return marker


class TestBackOnlineAnnounce:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        async def _skip(_delay: float) -> None:
            return None

        monkeypatch.setattr(ctrl.asyncio, "sleep", _skip)
        # No runtime bound in these tests — the announcer must not wait for
        # a ready signal that will never come.
        monkeypatch.setattr(ctrl, "_READY_EVENT", None)

    async def test_prior_marker_announced_and_removed(self, monkeypatch, tmp_path) -> None:
        import time as _time

        marker_ts = _time.time() - 30  # written by a PRIOR process
        marker = tmp_path / "platform_action.json"
        marker.write_text(
            json.dumps(
                {
                    "action": "restart",
                    "ts": marker_ts,
                    "notify": {"agent": "a", "channel": "telegram", "target": "42"},
                }
            )
        )
        monkeypatch.setattr(ctrl, "_marker_path", lambda: marker)
        monkeypatch.setattr(ctrl, "_BOOT_WALLTIME", _time.time())  # booted after the marker
        sent: list[tuple[Notify | None, str]] = []

        async def _capture(notify, text) -> bool:
            sent.append((notify, text))
            return True

        monkeypatch.setattr(ctrl, "send_followup", _capture)
        await ctrl._announce_restart_if_marked()
        assert not marker.exists()
        assert len(sent) == 1
        assert "Back online" in sent[0][1]
        assert sent[0][0].target == "42"

    async def test_own_pending_marker_not_announced(self, monkeypatch, tmp_path) -> None:
        # A marker whose ts is at/after this boot was written by a restart this
        # process just scheduled — must NOT fire a premature "back online".
        import time as _time

        now = _time.time()
        marker = tmp_path / "platform_action.json"
        marker.write_text(
            json.dumps(
                {
                    "action": "restart",
                    "ts": now + 5,
                    "notify": {"agent": "a", "channel": "telegram", "target": "42"},
                }
            )
        )
        monkeypatch.setattr(ctrl, "_marker_path", lambda: marker)
        monkeypatch.setattr(ctrl, "_BOOT_WALLTIME", now)
        sent = MagicMock()
        monkeypatch.setattr(ctrl, "send_followup", sent)
        await ctrl._announce_restart_if_marked()
        sent.assert_not_called()
        assert marker.exists()  # left for the next boot to consume

    async def test_stale_marker_ignored(self, monkeypatch, tmp_path) -> None:
        marker = tmp_path / "platform_action.json"
        marker.write_text(json.dumps({"action": "restart", "ts": 1, "notify": None}))
        monkeypatch.setattr(ctrl, "_marker_path", lambda: marker)
        monkeypatch.setattr(ctrl, "_BOOT_WALLTIME", None)
        sent = MagicMock()
        monkeypatch.setattr(ctrl, "send_followup", sent)
        await ctrl._announce_restart_if_marked()
        sent.assert_not_called()

    async def test_retries_until_channel_ready(self, monkeypatch, tmp_path) -> None:
        import time as _time

        marker = _write_restart_marker(
            tmp_path,
            ts=_time.time() - 30,
            notify={"agent": "a", "channel": "telegram", "target": "42"},
        )
        monkeypatch.setattr(ctrl, "_marker_path", lambda: marker)
        monkeypatch.setattr(ctrl, "_BOOT_WALLTIME", _time.time())
        attempts = {"n": 0}

        async def _flaky(notify, text) -> bool:
            attempts["n"] += 1
            return attempts["n"] >= 3  # channel only ready on the 3rd try

        monkeypatch.setattr(ctrl, "send_followup", _flaky)
        await ctrl._announce_restart_if_marked()
        assert attempts["n"] == 3

    async def test_gives_up_after_max_attempts_with_warning(
        self, monkeypatch, tmp_path, caplog
    ) -> None:
        import logging as _logging
        import time as _time

        marker = _write_restart_marker(
            tmp_path,
            ts=_time.time() - 30,
            notify={"agent": "a", "channel": "telegram", "target": "42"},
        )
        monkeypatch.setattr(ctrl, "_marker_path", lambda: marker)
        monkeypatch.setattr(ctrl, "_BOOT_WALLTIME", _time.time())
        attempts = {"n": 0}

        async def _never(notify, text) -> bool:
            attempts["n"] += 1
            return False

        monkeypatch.setattr(ctrl, "send_followup", _never)
        with caplog.at_level(_logging.WARNING, logger=ctrl.__name__):
            await ctrl._announce_restart_if_marked()
        assert attempts["n"] == ctrl._ANNOUNCE_MAX_ATTEMPTS
        assert any(
            "gave up delivering the restart notice" in rec.getMessage() for rec in caplog.records
        )

    async def test_slow_boot_marker_not_stale(self, monkeypatch, tmp_path) -> None:
        # Staleness is judged against boot time, not delivery time: a marker
        # 30s older than boot stays announceable no matter how long the boot
        # takes after that (here the announcer runs 700s past boot, which the
        # old delivery-time check would have discarded as stale).
        import time as _time

        boot = _time.time() - 700
        marker = _write_restart_marker(
            tmp_path,
            ts=boot - 30,
            notify={"agent": "a", "channel": "telegram", "target": "42"},
        )
        monkeypatch.setattr(ctrl, "_marker_path", lambda: marker)
        monkeypatch.setattr(ctrl, "_BOOT_WALLTIME", boot)
        sent: list[str] = []

        async def _capture(notify, text) -> bool:
            sent.append(text)
            return True

        monkeypatch.setattr(ctrl, "send_followup", _capture)
        await ctrl._announce_restart_if_marked()
        assert len(sent) == 1


class TestAnnounceReadyGating:
    """The announcer holds delivery until the runtime signals ready."""

    @pytest.fixture(autouse=True)
    def _fast_retries(self, monkeypatch):
        monkeypatch.setattr(ctrl, "_ANNOUNCE_RETRY_DELAY", 0.001)

    def _arm(self, monkeypatch, tmp_path: Path) -> tuple[Path, list[str]]:
        import time as _time

        marker = _write_restart_marker(
            tmp_path,
            ts=_time.time() - 30,
            notify={"agent": "a", "channel": "telegram", "target": "42"},
        )
        monkeypatch.setattr(ctrl, "_marker_path", lambda: marker)
        monkeypatch.setattr(ctrl, "_BOOT_WALLTIME", _time.time())
        sent: list[str] = []

        async def _capture(notify, text) -> bool:
            sent.append(text)
            return True

        monkeypatch.setattr(ctrl, "send_followup", _capture)
        return marker, sent

    async def test_holds_delivery_until_runtime_ready(self, monkeypatch, tmp_path) -> None:
        marker, sent = self._arm(monkeypatch, tmp_path)
        monkeypatch.setattr(ctrl, "_READY_EVENT", asyncio.Event())
        task = asyncio.get_running_loop().create_task(ctrl._announce_restart_if_marked())
        # The marker is consumed just before the ready-wait; once it's gone
        # (and ready is unset) the task is parked at the gate.
        for _ in range(500):
            if not marker.exists():
                break
            await asyncio.sleep(0.01)
        assert not marker.exists()
        assert sent == []  # held while boot is still in flight
        ctrl.notify_runtime_ready()
        await asyncio.wait_for(task, timeout=2)
        assert len(sent) == 1
        assert "Back online" in sent[0]

    async def test_ready_timeout_still_attempts_delivery(self, monkeypatch, tmp_path) -> None:
        _, sent = self._arm(monkeypatch, tmp_path)
        monkeypatch.setattr(ctrl, "_READY_EVENT", asyncio.Event())  # never set
        monkeypatch.setattr(ctrl, "_READY_WAIT_TIMEOUT", 0.01)
        await asyncio.wait_for(ctrl._announce_restart_if_marked(), timeout=2)
        assert len(sent) == 1

    async def test_already_ready_delivers_immediately(self, monkeypatch, tmp_path) -> None:
        _, sent = self._arm(monkeypatch, tmp_path)
        ready = asyncio.Event()
        ready.set()
        monkeypatch.setattr(ctrl, "_READY_EVENT", ready)
        await asyncio.wait_for(ctrl._announce_restart_if_marked(), timeout=2)
        assert len(sent) == 1

    async def test_bind_runtime_arms_ready_event(self, monkeypatch, tmp_path) -> None:
        # bind_runtime creates the event notify_runtime_ready() releases.
        for name in ("_RUNTIME", "_STARTED_AT", "_BOOT_WALLTIME", "_ANNOUNCE_TASK", "_READY_EVENT"):
            monkeypatch.setattr(ctrl, name, getattr(ctrl, name))  # restore after test
        monkeypatch.setattr(ctrl, "_marker_path", lambda: tmp_path / "absent.json")
        ctrl.bind_runtime(types.SimpleNamespace())
        assert ctrl._READY_EVENT is not None
        assert not ctrl._READY_EVENT.is_set()
        ctrl.notify_runtime_ready()
        assert ctrl._READY_EVENT.is_set()
        assert ctrl._ANNOUNCE_TASK is not None
        await asyncio.wait_for(ctrl._ANNOUNCE_TASK, timeout=2)


# ─────────────────────────── confirm store ──────────────────────────────────


class TestConfirmStore:
    def _ctx(self, base_config) -> object:
        from olano.cli.commands import CommandContext
        from olano.platforms.inbound import SenderInfo

        return CommandContext(
            config=base_config,
            runner=MagicMock(),
            thread_id="t-1",
            sender=SenderInfo(
                user_id="42",
                chat_id="chat-42",
                channel="telegram",
                is_trusted=True,
                allowlist_configured=True,
            ),
        )

    def test_stage_then_take(self, base_config) -> None:
        from olano.platform_control import commands as pc

        pc._PENDING.clear()
        ctx = self._ctx(base_config)
        pc._stage(ctx, "restart", "server")
        pending = pc._take_pending(ctx, "restart")
        assert pending is not None and pending.target == "server"
        # consumed — a second confirm finds nothing
        assert pc._take_pending(ctx, "restart") is None

    def test_action_mismatch(self, base_config) -> None:
        from olano.platform_control import commands as pc

        pc._PENDING.clear()
        ctx = self._ctx(base_config)
        pc._stage(ctx, "shutdown")
        assert pc._take_pending(ctx, "restart") is None

    def test_expiry(self, base_config) -> None:
        from olano.platform_control import commands as pc

        pc._PENDING.clear()
        ctx = self._ctx(base_config)
        pc._stage(ctx, "update")
        pc._PENDING[pc._pending_key(ctx)].expires = 0.0
        assert pc._take_pending(ctx, "update") is None

    def test_key_isolates_chats(self, base_config) -> None:
        from olano.cli.commands import CommandContext
        from olano.platform_control import commands as pc
        from olano.platforms.inbound import SenderInfo

        pc._PENDING.clear()
        ctx_a = self._ctx(base_config)
        ctx_b = CommandContext(
            config=base_config,
            runner=MagicMock(),
            thread_id="t-2",
            sender=SenderInfo(
                user_id="42",
                chat_id="other-chat",
                channel="telegram",
                is_trusted=True,
                allowlist_configured=True,
            ),
        )
        pc._stage(ctx_a, "shutdown")
        assert pc._take_pending(ctx_b, "shutdown") is None
        assert pc._take_pending(ctx_a, "shutdown") is not None


# ─────────────────────────── update strategies ──────────────────────────────


class TestUpdateStrategies:
    def test_docker_check_not_applicable(self, monkeypatch, tmp_path) -> None:
        from olano.update import strategies

        monkeypatch.setattr(strategies, "detect_install_mode", lambda home: InstallMode.DOCKER)
        check = strategies.check_update(tmp_path)
        assert check.can_apply is False
        assert "docker compose build" in check.details

    def test_binary_check_stub(self, monkeypatch, tmp_path) -> None:
        from olano.update import strategies

        monkeypatch.setattr(strategies, "detect_install_mode", lambda home: InstallMode.BINARY)
        check = strategies.check_update(tmp_path)
        assert check.can_apply is False
        assert "binary" in check.details.lower()

    def test_source_check_reports_commits_behind(self, monkeypatch, tmp_path) -> None:
        from olano.update import strategies

        monkeypatch.setattr(strategies, "detect_install_mode", lambda home: InstallMode.SOURCE)
        monkeypatch.setattr(strategies, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(strategies, "source_commit", lambda: "abc1234")
        outputs = {
            ("git", "fetch", "--quiet"): (0, ""),
            ("git", "rev-list", "--count", "HEAD..@{u}"): (0, "3"),
            ("git", "log", "-1", "--format=%h %s", "@{u}"): (0, "def5678 fix things"),
        }
        monkeypatch.setattr(
            strategies, "_run", lambda cmd, **kw: outputs.get(tuple(cmd), (1, "unexpected"))
        )
        check = strategies.check_update(tmp_path)
        assert check.update_available is True
        assert check.can_apply is True
        assert "3 new commit(s)" in check.details

    def test_source_check_up_to_date(self, monkeypatch, tmp_path) -> None:
        from olano.update import strategies

        monkeypatch.setattr(strategies, "detect_install_mode", lambda home: InstallMode.SOURCE)
        monkeypatch.setattr(strategies, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(strategies, "source_commit", lambda: "abc1234")
        outputs = {
            ("git", "fetch", "--quiet"): (0, ""),
            ("git", "rev-list", "--count", "HEAD..@{u}"): (0, "0"),
        }
        monkeypatch.setattr(
            strategies, "_run", lambda cmd, **kw: outputs.get(tuple(cmd), (1, "unexpected"))
        )
        check = strategies.check_update(tmp_path)
        assert check.update_available is False
        assert check.can_apply is True

    def test_source_apply_never_calls_uv_tool(self, monkeypatch, tmp_path) -> None:
        """The old engine ran `uv tool upgrade` on git checkouts — regression."""
        from olano.update import strategies

        calls: list[list[str]] = []

        def _fake_run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "status"]:
                return 0, ""
            return 0, "ok"

        monkeypatch.setattr(strategies, "detect_install_mode", lambda home: InstallMode.SOURCE)
        monkeypatch.setattr(strategies, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(strategies, "source_commit", lambda: "abc1234")
        monkeypatch.setattr(strategies, "_run", _fake_run)
        monkeypatch.setattr(strategies.shutil, "which", lambda name: None)
        result = strategies.upgrade_package(tmp_path)
        assert result.ok is True
        assert ["git", "pull", "--ff-only"] in calls
        assert not any(c[:3] == ["uv", "tool", "upgrade"] for c in calls)

    def test_source_apply_refuses_dirty_tree(self, monkeypatch, tmp_path) -> None:
        from olano.update import strategies

        def _fake_run(cmd, **kw):
            if cmd[:2] == ["git", "status"]:
                return 0, " M olano/core/runner.py"
            return 0, ""

        monkeypatch.setattr(strategies, "detect_install_mode", lambda home: InstallMode.SOURCE)
        monkeypatch.setattr(strategies, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(strategies, "_run", _fake_run)
        result = strategies.upgrade_package(tmp_path)
        assert result.ok is False
        assert "local modifications" in result.summary

    def test_uv_tool_apply(self, monkeypatch, tmp_path) -> None:
        from olano.update import strategies

        calls: list[list[str]] = []
        monkeypatch.setattr(strategies, "detect_install_mode", lambda home: InstallMode.UV_TOOL)
        monkeypatch.setattr(
            strategies, "_run", lambda cmd, **kw: (calls.append(list(cmd)), (0, "ok"))[1]
        )
        result = strategies.upgrade_package(tmp_path)
        assert result.ok is True
        assert ["uv", "tool", "upgrade", "olano"] in calls

    def test_source_check_survives_git_stderr_warning(self, monkeypatch, tmp_path) -> None:
        """git rev-list can print a stderr hint alongside the count; parsing the
        concatenated blob must not crash on int()."""
        from olano.update import strategies

        monkeypatch.setattr(strategies, "detect_install_mode", lambda home: InstallMode.SOURCE)
        monkeypatch.setattr(strategies, "repo_root", lambda: tmp_path)
        monkeypatch.setattr(strategies, "source_commit", lambda: "abc1234")
        outputs = {
            ("git", "fetch", "--quiet"): (0, ""),
            # count on stdout, deprecation hint concatenated from stderr
            ("git", "rev-list", "--count", "HEAD..@{u}"): (0, "2\nhint: grafts is deprecated"),
            ("git", "log", "-1", "--format=%h %s", "@{u}"): (0, "def5678 fix"),
        }
        monkeypatch.setattr(
            strategies, "_run", lambda cmd, **kw: outputs.get(tuple(cmd), (1, "unexpected"))
        )
        check = strategies.check_update(tmp_path)
        assert check.update_available is True
        assert "2 new commit(s)" in check.details


class TestSenderTrust:
    """The owner gate must key on stable ids, not the settable display name."""

    def test_display_name_never_trusted(self) -> None:
        from olano.runtime.server import _sender_is_trusted

        allow = frozenset({"kevin"})  # an @handle / name string
        # An attacker whose id/handle differ but who set their display name to
        # 'kevin' is NOT trusted (display_name isn't even an input to the gate).
        assert _sender_is_trusted("999", "attacker", allow) is False
        # The real kevin (matching handle) is trusted.
        assert _sender_is_trusted("999", "kevin", allow) is True
        # Numeric id match also works.
        assert _sender_is_trusted("12345", "", frozenset({"12345"})) is True

    def test_empty_allowlist_trusts_nobody(self) -> None:
        from olano.runtime.server import _sender_is_trusted

        assert _sender_is_trusted("12345", "boss", frozenset()) is False

    def test_platform_handler_trust_never_matches_display_name(self) -> None:
        """The platform inbound handler must derive is_trusted from
        _sender_is_trusted (stable ids only). A previous inline check also
        matched display_name, which let any group member rename themselves to
        an allowlisted name and receive HITL auto-approval + ctx.is_owner."""
        import inspect

        from olano.runtime.server import RuntimeServer

        src = inspect.getsource(RuntimeServer._boot_platform_adapter)
        assert "_sender_is_trusted(user_id, handle, trusted_users, phone=phone)" in src
        # The WhatsApp phone is the sender's phone JID (or its id), never a name.
        assert 'phone = str(getattr(source, "user_id_alt", "") or "") or user_id' in src
        assert "display_name in allowed_users" not in src
        assert "display_name in trusted_users" not in src

    def test_platform_handler_unions_owner_users_into_trust(self) -> None:
        """owner_users designates owners without gating admission; the platform
        path must union it into the trust set AND into allowlist_configured,
        so an open bot with only owner_users set envelopes non-owners."""
        import inspect

        from olano.runtime.server import RuntimeServer

        src = inspect.getsource(RuntimeServer._boot_platform_adapter)
        assert 'extra.get("owner_users")' in src
        assert "allowlist_configured = bool(allowed_users | owner_users)" in src
        assert "allowlist_configured=allowlist_configured" in src

    def test_business_whatsapp_allowlists_admit_customers_not_owners(self) -> None:
        """On Twilio / Meta, ``allowed_users`` is a customer list: matching it
        by phone number must not make every allowed customer an owner."""
        import inspect

        from olano.runtime.server import _CUSTOMER_ALLOWLIST_PLATFORMS, RuntimeServer

        assert _CUSTOMER_ALLOWLIST_PLATFORMS == {"whatsapp_twilio", "whatsapp_meta_api"}
        src = inspect.getsource(RuntimeServer._boot_platform_adapter)
        assert "if platform_enum.value in _CUSTOMER_ALLOWLIST_PLATFORMS:" in src
        assert "trusted_users = owner_users" in src


class TestNormalizeAllowedUsers:
    def test_strip_before_at(self) -> None:
        from olano.runtime.server import _normalize_allowed_users

        # ' @Boss ' must normalize to 'boss' (strip first, then drop the @).
        assert _normalize_allowed_users([" @Boss "]) == frozenset({"boss"})
        assert _normalize_allowed_users("@Kevin") == frozenset({"kevin"})
        assert _normalize_allowed_users([12345, "  "]) == frozenset({"12345"})
        assert _normalize_allowed_users(None) == frozenset()
