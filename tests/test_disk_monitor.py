"""Disk monitor + automatic cleanup: settings, engine tick, actions, API.

The loop itself is never run (scheduler-test convention) — every test drives
``disk_monitor_tick`` / ``run_cleanup`` directly with injected ``now``,
``usage_fn``, ``which_fn`` and ``run_fn`` fakes, and asserts on the recorded
subprocess argvs. The highest-value property for a destructive feature: **no
command ever runs below the threshold, and nothing outside the fixed
allow-list — or deselected in config — ever runs at all.**
"""

from __future__ import annotations

import subprocess
import threading
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from olano.config.system_settings import (
    DISK_CLEANUP_ACTIONS,
    DISK_CLEANUP_SAFE_ACTIONS,
    DiskMonitorSettings,
    load_disk_monitor_settings,
    set_disk_cleanup_enabled,
    set_disk_monitor_settings,
)
from olano.runtime import disk_monitor as dm

Usage = namedtuple("Usage", "total used free")
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def usage_at(percent: float, total: int = 1000):
    """A usage_fn returning a fixed used/(used+free) percentage (no root reserve)."""
    used = int(total * percent / 100)
    return lambda _path: Usage(total, used, total - used)


def recording_run(rc: int = 0, stdout: str = "", stderr: str = ""):
    """A run_fn that records argvs and returns a fixed CompletedProcess."""
    calls: list[list[str]] = []

    def _run(argv, **_kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

    return _run, calls


def which_all(binary: str) -> str:
    return f"/usr/bin/{binary}"


def tick(settings: DiskMonitorSettings, state: dict, tmp_path, **kw):
    """disk_monitor_tick with safe test defaults for every injected dependency."""
    kw.setdefault("now", NOW)
    kw.setdefault("usage_fn", usage_at(40))
    kw.setdefault("which_fn", which_all)
    kw.setdefault("run_fn", recording_run()[0])
    kw.setdefault("in_container_fn", lambda: False)
    kw.setdefault("docker_fn", lambda: True)  # hermetic: never probe the real socket
    return dm.disk_monitor_tick(settings=settings, state=state, home=tmp_path, **kw)


# ── settings: clamps, coercion, safety defaults ───────────────────────────────


def test_settings_defaults_select_every_action():
    s = DiskMonitorSettings()
    assert s.enabled is True
    assert s.warn_percent == 80
    assert s.cleanup_percent == 85
    assert s.cleanup.enabled is True
    # Every registry action is on by default — docker_prune included (it keeps
    # its "aggressive" badge; deselecting it is the opt-out).
    assert s.cleanup.actions == list(DISK_CLEANUP_ACTIONS)
    assert "docker_prune" in s.cleanup.actions


def test_settings_clamp_and_coerce_garbage():
    s = DiskMonitorSettings.model_validate(
        {
            "warn_percent": 300,
            "cleanup_percent": 10,
            "check_interval_minutes": "abc",
            "path": "",
            "cleanup": {
                "journald_vacuum_size": "; rm -rf /",
                "actions": ["docker_prune", "evil", "npm_cache"],
                "cooldown_minutes": 1,
                "force_interval_days": 9999,
            },
        }
    )
    assert s.warn_percent == 99
    assert s.cleanup_percent == 99  # coerced up to warn
    assert s.check_interval_minutes == 30
    assert s.path == "/"
    assert s.cleanup.journald_vacuum_size == "200M"  # injection attempt → default
    assert s.cleanup.actions == ["docker_prune", "npm_cache"]  # registry order, no 'evil'
    assert s.cleanup.cooldown_minutes == 30
    assert s.cleanup.force_interval_days == 90


def test_settings_normalize_legacy_action_name():
    """Configs saved before the lionclaw_caches → olano_caches rename keep the
    action — dropping it would silently turn housekeeping cleanup off."""
    s = DiskMonitorSettings.model_validate(
        {"cleanup": {"actions": ["lionclaw_caches", "npm_cache"]}}
    )
    assert s.cleanup.actions == ["olano_caches", "npm_cache"]


def test_settings_loader_survives_malformed_yaml(lionclaw_home):
    config = lionclaw_home / "config.yaml"
    config.write_text(":: not yaml ::{", encoding="utf-8")
    s = load_disk_monitor_settings()
    assert s.enabled is True and s.cleanup.enabled is True


def test_set_disk_monitor_settings_preserves_unrelated_keys(lionclaw_home):
    config = lionclaw_home / "config.yaml"
    config.write_text("foo: bar\nplayground:\n  enabled: true\n", encoding="utf-8")
    set_disk_monitor_settings({"warn_percent": 70, "cleanup": {"enabled": False}})
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert raw["foo"] == "bar"
    assert raw["playground"]["enabled"] is True
    assert raw["disk_monitor"]["warn_percent"] == 70
    assert raw["disk_monitor"]["cleanup"]["enabled"] is False
    # Round-trip loads back clamped/validated.
    assert load_disk_monitor_settings().warn_percent == 70


def test_set_disk_cleanup_enabled_flips_only_the_opt_in(lionclaw_home):
    set_disk_cleanup_enabled(False)
    s = load_disk_monitor_settings()
    assert s.cleanup.enabled is False
    assert s.enabled is True  # monitoring untouched
    set_disk_cleanup_enabled(True)
    assert load_disk_monitor_settings().cleanup.enabled is True


def test_registry_matches_config_allow_list():
    assert tuple(dm.ACTION_REGISTRY) == DISK_CLEANUP_ACTIONS
    safe = {s.name for s in dm.ACTION_REGISTRY.values() if s.safe}
    assert safe == set(DISK_CLEANUP_SAFE_ACTIONS)
    assert dm.ACTION_REGISTRY["docker_prune"].safe is False


# ── tick: alerts + thresholds ─────────────────────────────────────────────────


def test_below_threshold_runs_nothing(tmp_path):
    run_fn, calls = recording_run()
    alerts: list[str] = []
    state, snap, report = tick(
        DiskMonitorSettings(),
        dm.default_state(),
        tmp_path,
        usage_fn=usage_at(40),
        run_fn=run_fn,
        alert_fn=lambda kind, _p: alerts.append(kind),
    )
    assert calls == [] and alerts == [] and report is None
    assert state["level"] == "ok" and snap.level == "ok"


def test_warn_alerts_once_then_realerts_after_window(tmp_path):
    alerts: list[str] = []
    settings = DiskMonitorSettings()
    state = dm.default_state()
    kw = dict(usage_fn=usage_at(82), alert_fn=lambda kind, _p: alerts.append(kind))
    state, _, _ = tick(settings, state, tmp_path, now=NOW, **kw)
    assert alerts == ["warn"]
    state, _, _ = tick(settings, state, tmp_path, now=NOW + timedelta(minutes=30), **kw)
    assert alerts == ["warn"]  # inside the re-alert window — no spam
    state, _, _ = tick(settings, state, tmp_path, now=NOW + timedelta(hours=7), **kw)
    assert alerts == ["warn", "warn"]  # re-alerted after REALERT_MINUTES
    # Recovery fires a single "ok" note and clears the alert clock.
    state, _, _ = tick(
        settings,
        state,
        tmp_path,
        now=NOW + timedelta(hours=8),
        usage_fn=usage_at(40),
        alert_fn=lambda kind, _p: alerts.append(kind),
    )
    assert alerts == ["warn", "warn", "ok"]
    assert state["level"] == "ok" and state["last_alert_at"] is None


def test_critical_with_cleanup_disabled_never_runs_commands(tmp_path):
    """The safety property: alerts fire, but nothing executes."""
    run_fn, calls = recording_run()
    payloads: list[dict] = []
    settings = DiskMonitorSettings.model_validate({"cleanup": {"enabled": False}})
    state, _, report = tick(
        DiskMonitorSettings.model_validate(settings.model_dump()),
        dm.default_state(),
        tmp_path,
        usage_fn=usage_at(92),
        run_fn=run_fn,
        alert_fn=lambda kind, p: payloads.append({"kind": kind, **p}),
    )
    assert calls == [] and report is None
    assert [p["kind"] for p in payloads] == ["critical"]
    assert "olano disk --clean" in payloads[0]["message"]  # actionable hint
    assert state["last_cleanup_at"] is None


def test_critical_runs_default_actions_in_registry_order(tmp_path):
    run_fn, calls = recording_run(stdout="Vacuuming done, freed 600.0M of archived journals")
    alerts: list[str] = []
    state, _, report = tick(
        DiskMonitorSettings(),
        dm.default_state(),
        tmp_path,
        usage_fn=usage_at(90),
        run_fn=run_fn,
        alert_fn=lambda kind, _p: alerts.append(kind),
    )
    # Command actions in registry order (olano_caches / revisions_gc /
    # olano_stale_images are internal). docker_prune (-af) is part of the
    # default set now, running between the dangling-only image prune and npm.
    assert calls == [
        ["docker", "image", "prune", "-f"],
        ["docker", "system", "prune", "-af"],
        ["npm", "cache", "clean", "--force"],
        ["journalctl", "--vacuum-size=200M"],
    ]
    assert alerts == ["critical", "cleanup"]
    assert report is not None and report.trigger == "threshold"
    assert state["last_cleanup_at"] == NOW.isoformat()
    assert state["last_report"]["results"][0]["name"] == "olano_caches"
    journald = next(r for r in report.results if r.name == "journald_vacuum")
    assert journald.reported.startswith("Vacuuming done")


def test_docker_prune_never_runs_when_deselected(tmp_path):
    """Deselecting the aggressive action must stick — an explicit actions list
    without docker_prune runs everything else and never `docker system prune`."""
    settings = DiskMonitorSettings.model_validate(
        {"cleanup": {"actions": [a for a in DISK_CLEANUP_ACTIONS if a != "docker_prune"]}}
    )
    run_fn, calls = recording_run()
    _, _, report = tick(
        settings, dm.default_state(), tmp_path, usage_fn=usage_at(90), run_fn=run_fn
    )
    assert ["docker", "system", "prune", "-af"] not in calls
    assert ["npm", "cache", "clean", "--force"] in calls
    assert not any(r.name == "docker_prune" for r in report.results)


def test_docker_prune_runs_when_selected(tmp_path):
    settings = DiskMonitorSettings.model_validate(
        {"cleanup": {"actions": ["docker_prune", "npm_cache"]}}
    )
    run_fn, calls = recording_run(stdout="Total reclaimed space: 11.6GB")
    _, _, report = tick(
        settings, dm.default_state(), tmp_path, usage_fn=usage_at(90), run_fn=run_fn
    )
    assert calls == [
        ["docker", "system", "prune", "-af"],
        ["npm", "cache", "clean", "--force"],
    ]
    docker = next(r for r in report.results if r.name == "docker_prune")
    assert docker.reported == "Total reclaimed space: 11.6GB"


def test_vacuum_size_setting_reaches_the_argv(tmp_path):
    settings = DiskMonitorSettings.model_validate(
        {"cleanup": {"actions": ["journald_vacuum"], "journald_vacuum_size": "1G"}}
    )
    run_fn, calls = recording_run()
    tick(settings, dm.default_state(), tmp_path, usage_fn=usage_at(90), run_fn=run_fn)
    assert calls == [["journalctl", "--vacuum-size=1G"]]


# ── tick: cooldown + forced interval ─────────────────────────────────────────


def test_cooldown_blocks_rerun_until_elapsed(tmp_path):
    run_fn, calls = recording_run()
    settings = DiskMonitorSettings()  # cooldown 360 min
    state = dm.default_state()
    kw = dict(usage_fn=usage_at(90), run_fn=run_fn)
    state, _, r1 = tick(settings, state, tmp_path, now=NOW, **kw)
    assert r1 is not None
    ran_after_first = len(calls)
    state, _, r2 = tick(settings, state, tmp_path, now=NOW + timedelta(hours=1), **kw)
    assert r2 is None and len(calls) == ran_after_first  # still cooling down
    state, _, r3 = tick(settings, state, tmp_path, now=NOW + timedelta(hours=7), **kw)
    assert r3 is not None and len(calls) > ran_after_first


def test_force_interval_starts_clock_then_fires(tmp_path):
    run_fn, calls = recording_run()
    settings = DiskMonitorSettings.model_validate({"cleanup": {"force_interval_days": 1}})
    state = dm.default_state()
    kw = dict(usage_fn=usage_at(40), run_fn=run_fn)  # never above threshold
    state, _, r1 = tick(settings, state, tmp_path, now=NOW, **kw)
    assert r1 is None  # first observation only starts the clock
    assert state["last_forced_cleanup_at"] == NOW.isoformat()
    state, _, r2 = tick(settings, state, tmp_path, now=NOW + timedelta(hours=23), **kw)
    assert r2 is None
    state, _, r3 = tick(settings, state, tmp_path, now=NOW + timedelta(hours=25), **kw)
    assert r3 is not None and r3.trigger == "force_interval"
    assert calls  # commands actually ran


# ── executor: skip / fallback / guard behavior ───────────────────────────────


def test_missing_binary_skips_action_but_not_the_rest(tmp_path):
    run_fn, calls = recording_run()
    _, _, report = tick(
        DiskMonitorSettings(),
        dm.default_state(),
        tmp_path,
        usage_fn=usage_at(90),
        which_fn=lambda b: None if b == "npm" else which_all(b),
        run_fn=run_fn,
    )
    npm = next(r for r in report.results if r.name == "npm_cache")
    assert npm.status == "skipped" and npm.reason == "npm not installed"
    assert ["journalctl", "--vacuum-size=200M"] in calls
    assert not any(c[0] == "npm" for c in calls)


def test_journald_permission_failure_falls_back_to_sudo(tmp_path):
    calls: list[list[str]] = []

    def run_fn(argv, **_kw):
        calls.append(list(argv))
        if argv[0] == "sudo":
            return subprocess.CompletedProcess(
                argv, 0, stdout="Vacuuming done, freed 600M", stderr=""
            )
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Failed: Permission denied")

    settings = DiskMonitorSettings.model_validate({"cleanup": {"actions": ["journald_vacuum"]}})
    _, _, report = tick(
        settings, dm.default_state(), tmp_path, usage_fn=usage_at(90), run_fn=run_fn
    )
    assert calls == [
        ["journalctl", "--vacuum-size=200M"],
        ["sudo", "-n", "journalctl", "--vacuum-size=200M"],
    ]
    result = report.results[0]
    assert result.status == "ok" and result.used_sudo is True


def test_journald_fails_cleanly_when_sudo_also_denied(tmp_path):
    run_fn, calls = recording_run(rc=1, stderr="journalctl: Permission denied")
    settings = DiskMonitorSettings.model_validate({"cleanup": {"actions": ["journald_vacuum"]}})
    _, _, report = tick(
        settings, dm.default_state(), tmp_path, usage_fn=usage_at(90), run_fn=run_fn
    )
    result = report.results[0]
    assert result.status == "failed" and result.reason == "permission denied"
    assert len(calls) == 2  # tried plain + sudo -n, then gave up


def test_docker_actions_skip_without_a_reachable_daemon(tmp_path):
    settings = DiskMonitorSettings.model_validate(
        {"cleanup": {"actions": ["docker_image_prune", "docker_prune"]}}
    )
    run_fn, calls = recording_run()
    _, _, report = tick(
        settings,
        dm.default_state(),
        tmp_path,
        usage_fn=usage_at(90),
        run_fn=run_fn,
        docker_fn=lambda: False,
    )
    assert calls == []
    assert {r.status for r in report.results} == {"skipped"}
    assert {r.reason for r in report.results} == {"docker daemon not reachable"}


def test_docker_actions_run_inside_a_container_when_socket_is_mounted(tmp_path):
    """The all-in-one deployment bind-mounts /var/run/docker.sock into the
    runtime container — being *in* a container must not block the prunes
    (that blanket guard is what made the dashboard's Clean now a no-op)."""
    settings = DiskMonitorSettings.model_validate({"cleanup": {"actions": ["docker_prune"]}})
    run_fn, calls = recording_run(stdout="Total reclaimed space: 11.6GB")
    _, _, report = tick(
        settings,
        dm.default_state(),
        tmp_path,
        usage_fn=usage_at(90),
        run_fn=run_fn,
        in_container_fn=lambda: True,
        docker_fn=lambda: True,
    )
    assert calls == [["docker", "system", "prune", "-af"]]
    assert report.results[0].status == "ok"


def test_scan_gating_daily_and_only_stamped_on_real_start(tmp_path):
    settings = DiskMonitorSettings()
    state = dm.default_state()
    calls: list[str] = []

    # Never scanned → starts immediately, stamps last_scan_at.
    state, _, _ = tick(settings, state, tmp_path, scan_fn=lambda: calls.append("a") or True)
    assert calls == ["a"] and state["last_scan_at"] == NOW.isoformat()

    # Within the interval → not called again.
    state, _, _ = tick(
        settings,
        state,
        tmp_path,
        now=NOW + timedelta(hours=23),
        scan_fn=lambda: calls.append("b") or True,
    )
    assert calls == ["a"]

    # Past the interval → called; a busy slot (False) leaves the stamp so the
    # next tick retries.
    state, _, _ = tick(
        settings,
        state,
        tmp_path,
        now=NOW + timedelta(hours=25),
        scan_fn=lambda: calls.append("c") or False,
    )
    assert calls == ["a", "c"]
    assert state["last_scan_at"] == NOW.isoformat()  # unchanged — scan didn't start


def test_scan_disabled_never_calls_scan_fn(tmp_path):
    settings = DiskMonitorSettings.model_validate({"scan": {"enabled": False}})
    called: list[str] = []
    tick(settings, dm.default_state(), tmp_path, scan_fn=lambda: called.append("x") or True)
    assert called == []


def test_one_failing_action_never_stops_the_rest(tmp_path):
    def run_fn(argv, **_kw):
        if argv[0] == "npm":
            raise subprocess.TimeoutExpired(argv, 300)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    _, _, report = tick(
        DiskMonitorSettings(),
        dm.default_state(),
        tmp_path,
        usage_fn=usage_at(90),
        run_fn=run_fn,
    )
    by_name = {r.name: r for r in report.results}
    assert by_name["npm_cache"].status == "failed"
    assert by_name["npm_cache"].reason == "timeout after 300s"
    assert by_name["journald_vacuum"].status == "ok"


# ── state persistence + monitor object ───────────────────────────────────────


def test_state_round_trip_and_corruption_recovery(tmp_path):
    path = dm.state_path(tmp_path)
    state = dm.default_state()
    state["last_cleanup_at"] = NOW.isoformat()
    dm.save_state(path, state)
    assert dm.load_state(path)["last_cleanup_at"] == NOW.isoformat()
    path.write_text("{corrupt", encoding="utf-8")
    assert dm.load_state(path) == dm.default_state()


def test_measure_disk_matches_df_semantics():
    settings = DiskMonitorSettings()
    # Root-reserved blocks: total 100, used 90, free-for-us 5 → df says 94.7%.
    snap = dm.measure_disk(settings, NOW, usage_fn=lambda _p: Usage(100, 90, 5))
    assert round(snap.percent, 1) == 94.7
    assert snap.level == "critical"


def test_monitor_from_env_kill_switch(monkeypatch):
    monkeypatch.setenv("LIONCLAW_NO_DISK_MONITOR", "1")
    assert dm.monitor_from_env().env_disabled is True
    monkeypatch.delenv("LIONCLAW_NO_DISK_MONITOR")
    assert dm.monitor_from_env().env_disabled is False


def test_disk_monitor_single_cleanup_slot():
    mon = dm.DiskMonitor()
    assert mon.try_begin_cleanup() is True
    assert mon.try_begin_cleanup() is False  # slot taken
    mon.end_cleanup()
    assert mon.try_begin_cleanup() is True


# ── API endpoints ─────────────────────────────────────────────────────────────


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    """TestClient over a mock runtime that carries a real DiskMonitor."""
    from fastapi.testclient import TestClient

    from olano.admin.memory import MemoryMonitor
    from olano.runtime.api import create_api

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))

    runner = MagicMock()
    runner.ainvoke = AsyncMock(return_value="hi")
    rt = MagicMock()
    rt._runners = {}
    rt._channels = []
    rt._memory_monitor = MemoryMonitor()
    rt._disk_monitor = dm.DiskMonitor()
    return TestClient(create_api(rt)), rt


def test_get_disk_diagnostics_shape(api_client):
    client, _rt = api_client
    resp = client.get("/api/diagnostics/disk")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] in ("local", "runtime")
    assert "percent" in data and "level" in data
    assert data["settings"]["warn_percent"] == 80
    names = [a["name"] for a in data["actions"]]
    assert names == list(DISK_CLEANUP_ACTIONS)
    # The whole-disk attribution rides along on the same call the dashboard
    # already polls — the card must never need a second request to explain the
    # gap between "61% used" and the home's directory list.
    breakdown = data["breakdown"]
    assert breakdown["used_bytes"] > 0
    keys = {g["key"] for g in breakdown["groups"]}
    assert {"docker_images", "olano_home", "system"} <= keys
    system = next(g for g in breakdown["groups"] if g["key"] == "system")
    assert system["detail"]  # the residual always explains itself


def test_disk_monitor_toggle_endpoint_persists(api_client, lionclaw_home):
    client, _rt = api_client
    (lionclaw_home / "config.yaml").write_text("foo: bar\n", encoding="utf-8")
    off = client.post("/api/diagnostics/disk/monitor", json={"action": "off"})
    assert off.status_code == 200 and off.json()["ok"] is True
    raw = yaml.safe_load((lionclaw_home / "config.yaml").read_text(encoding="utf-8"))
    assert raw["foo"] == "bar"  # unrelated keys preserved
    assert raw["disk_monitor"]["cleanup"]["enabled"] is False
    on = client.post("/api/diagnostics/disk/monitor", json={"action": "on"})
    assert on.status_code == 200
    assert load_disk_monitor_settings().cleanup.enabled is True


def test_disk_monitor_run_endpoint_uses_single_slot(api_client, monkeypatch):
    client, rt = api_client
    started, release = threading.Event(), threading.Event()

    def fake_cleanup(**kwargs):
        started.set()
        release.wait(timeout=5)
        return dm.CleanupReport(
            started_at=NOW.isoformat(),
            trigger="manual",
            path="/",
            percent_before=90.0,
            percent_after=70.0,
            freed_bytes_total=123,
        )

    monkeypatch.setattr("olano.runtime.disk_monitor.run_cleanup", fake_cleanup)
    first = client.post("/api/diagnostics/disk/monitor", json={"action": "run"})
    assert first.status_code == 200
    assert first.json()["ok"] is True and first.json()["running"] is True
    assert started.wait(timeout=5)
    second = client.post("/api/diagnostics/disk/monitor", json={"action": "run"})
    assert second.json()["ok"] is False
    assert "already running" in second.json()["error"]
    release.set()
    for _ in range(50):
        if not rt._disk_monitor.cleanup_running:
            break
        threading.Event().wait(0.1)
    assert rt._disk_monitor.cleanup_running is False
    assert rt._disk_monitor.last_report is not None
    assert rt._disk_monitor.last_report["freed_bytes_total"] == 123


def test_system_config_includes_disk_block(api_client):
    client, _rt = api_client
    data = client.get("/api/system/config").json()
    assert data["disk_monitor"]["cleanup_percent"] == 85
    catalog = {a["name"]: a for a in data["disk_cleanup_actions"]}
    assert catalog["docker_prune"]["safe"] is False
    assert catalog["olano_caches"]["safe"] is True
    # The legacy name is normalized away everywhere user-facing.
    assert "lionclaw_caches" not in catalog


def test_system_config_put_validates_disk_block(api_client):
    client, _rt = api_client
    bad_pct = client.put("/api/system/config", json={"disk_monitor": {"warn_percent": 200}})
    assert bad_pct.status_code == 400
    bad_order = client.put(
        "/api/system/config",
        json={"disk_monitor": {"warn_percent": 90, "cleanup_percent": 60}},
    )
    assert bad_order.status_code == 400
    bad_action = client.put(
        "/api/system/config",
        json={"disk_monitor": {"cleanup": {"actions": ["rm_rf_slash"]}}},
    )
    assert bad_action.status_code == 400
    bad_size = client.put(
        "/api/system/config",
        json={"disk_monitor": {"cleanup": {"journald_vacuum_size": "200M; reboot"}}},
    )
    assert bad_size.status_code == 400
    ok = client.put(
        "/api/system/config",
        json={
            "disk_monitor": {
                "warn_percent": 75,
                "cleanup_percent": 82,
                "cleanup": {"actions": ["olano_caches", "docker_prune"], "enabled": True},
            }
        },
    )
    assert ok.status_code == 200
    saved = load_disk_monitor_settings()
    assert saved.warn_percent == 75 and saved.cleanup_percent == 82
    assert saved.cleanup.actions == ["olano_caches", "docker_prune"]


def test_system_config_put_accepts_legacy_action_name(api_client):
    """A dashboard tab opened before the lionclaw_caches → olano_caches rename
    still saves — the legacy name is normalized, never rejected or dropped."""
    client, _rt = api_client
    ok = client.put(
        "/api/system/config",
        json={"disk_monitor": {"cleanup": {"actions": ["lionclaw_caches", "npm_cache"]}}},
    )
    assert ok.status_code == 200
    assert load_disk_monitor_settings().cleanup.actions == ["olano_caches", "npm_cache"]
