"""The checkpoint history sweep's runtime wiring: settings, loop, routes.

The collector itself is proven against a real Postgres in
``test_checkpoint_gc.py``; this file pins how the runtime drives it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.config.system_settings import CheckpointGcSettings, load_checkpoint_gc_settings
from olano.core import checkpoint_gc
from olano.runtime.server import RuntimeServer


def test_settings_defaults_and_clamps(lionclaw_home) -> None:
    assert load_checkpoint_gc_settings() == CheckpointGcSettings()
    (lionclaw_home / "config.yaml").write_text(
        "checkpoint_gc:\n  enabled: false\n  interval_hours: 0\n  keep_min: 99999\n"
        "  boot_delay_minutes: 2\n  max_minutes: abc\n",
        encoding="utf-8",
    )
    s = load_checkpoint_gc_settings()
    assert s.enabled is False
    assert s.interval_hours == 1  # clamped up from 0
    assert s.keep_min == 1000  # clamped down
    assert s.boot_delay_minutes == 2
    assert s.max_minutes == 30  # junk falls back to the default


def test_settings_survive_malformed_yaml(lionclaw_home) -> None:
    (lionclaw_home / "config.yaml").write_text(":: not yaml ::{", encoding="utf-8")
    assert load_checkpoint_gc_settings().enabled is True


def _stub_runtime() -> SimpleNamespace:
    return SimpleNamespace(_checkpoint_gc_last=None, _agents_booting=False)


async def test_run_checkpoint_gc_sweeps_the_saver_pool_and_keeps_the_report(
    monkeypatch, lionclaw_home
) -> None:
    pool = object()
    saver = SimpleNamespace(conn=pool)
    seen: dict[str, object] = {}

    async def fake_saver(home=None):
        return saver

    async def fake_collect(
        got_pool, *, keep_min, dry_run, max_seconds, saver=None, live_agents=None, **_
    ):
        seen.update(
            pool=got_pool,
            keep_min=keep_min,
            dry_run=dry_run,
            max_seconds=max_seconds,
            saver=saver,
            live_agents=live_agents,
        )
        return checkpoint_gc.GcReport(
            dry_run=dry_run, namespaces_scanned=4, namespaces_swept=2, checkpoints_removed=7
        )

    def fake_list_agents(*, include_hidden=False):
        # The sweep's orphan report must count a hidden agent as live, or its
        # threads read as abandoned.
        return ["cindy", "hidden-one"] if include_hidden else ["cindy"]

    monkeypatch.setattr("olano.core.checkpoints.aget_postgres_saver", fake_saver)
    monkeypatch.setattr("olano.config.loader.list_agents", fake_list_agents)
    monkeypatch.setattr(checkpoint_gc, "collect", fake_collect)
    (lionclaw_home / "config.yaml").write_text(
        "checkpoint_gc:\n  keep_min: 5\n  max_minutes: 2\n", encoding="utf-8"
    )
    rt = _stub_runtime()
    report = await RuntimeServer.run_checkpoint_gc(rt, dry_run=True)  # type: ignore[arg-type]
    # The sweep runs on the saver's own pool and gets the saver itself for the
    # compaction writes.
    assert seen == {
        "pool": pool,
        "keep_min": 5,
        "dry_run": True,
        "max_seconds": 120,
        "saver": saver,
        "live_agents": {"cindy", "hidden-one"},
    }
    assert report is not None and report["checkpoints_removed"] == 7
    assert rt._checkpoint_gc_last is report
    assert report["finished_at"] > 0


async def test_run_checkpoint_gc_is_a_no_op_on_sqlite(monkeypatch) -> None:
    async def no_saver(home=None):
        return None

    monkeypatch.setattr("olano.core.checkpoints.aget_postgres_saver", no_saver)
    rt = _stub_runtime()
    assert await RuntimeServer.run_checkpoint_gc(rt) is None  # type: ignore[arg-type]
    assert rt._checkpoint_gc_last is None


async def test_loop_is_skipped_by_the_env_switch(monkeypatch) -> None:
    monkeypatch.setenv("LIONCLAW_NO_CHECKPOINT_GC", "1")
    rt = _stub_runtime()
    rt.run_checkpoint_gc = AsyncMock()
    await asyncio.wait_for(RuntimeServer._checkpoint_gc_loop(rt), 5)  # type: ignore[arg-type]
    rt.run_checkpoint_gc.assert_not_called()


async def test_loop_runs_one_pass_then_exits_on_sqlite(monkeypatch, lionclaw_home) -> None:
    (lionclaw_home / "config.yaml").write_text(
        "checkpoint_gc:\n  boot_delay_minutes: 0\n", encoding="utf-8"
    )
    rt = _stub_runtime()
    rt.run_checkpoint_gc = AsyncMock(return_value=None)
    await asyncio.wait_for(RuntimeServer._checkpoint_gc_loop(rt), 5)  # type: ignore[arg-type]
    rt.run_checkpoint_gc.assert_awaited_once()


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    from fastapi.testclient import TestClient

    from olano.admin.memory import MemoryMonitor
    from olano.runtime.api import create_api

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    rt = MagicMock()
    rt._runners = {}
    rt._channels = []
    rt._memory_monitor = MemoryMonitor()
    rt._checkpoint_gc_last = None
    rt.run_checkpoint_gc = AsyncMock(return_value={"checkpoints_removed": 3})
    return TestClient(create_api(rt)), rt


def test_routes_on_sqlite_report_the_backend(api_client, monkeypatch) -> None:
    async def no_pool():
        return None

    monkeypatch.setattr("olano.core.checkpoints.aget_postgres_pool", no_pool)
    client, rt = api_client
    resp = client.get("/api/diagnostics/checkpoints")
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "sqlite" and body["tables"] is None
    assert body["settings"]["interval_hours"] == 6

    rt.run_checkpoint_gc = AsyncMock(return_value=None)
    resp = client.post("/api/diagnostics/checkpoints/gc", json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.json() == {"backend": "sqlite", "ran": False}
    rt.run_checkpoint_gc.assert_awaited_once_with(dry_run=True)


def test_gc_route_runs_a_sweep(api_client) -> None:
    client, rt = api_client
    resp = client.post("/api/diagnostics/checkpoints/gc", json={})
    assert resp.status_code == 200
    assert resp.json() == {
        "backend": "postgres",
        "ran": True,
        "report": {"checkpoints_removed": 3},
    }
    rt.run_checkpoint_gc.assert_awaited_once_with(dry_run=False)


def test_resnapshot_migration_is_discovered_and_skips_sqlite(lionclaw_home, monkeypatch) -> None:
    import importlib

    from olano.migrations import pending_migration_ids

    mig = importlib.import_module("olano.migrations.0029_checkpoint_history_resnapshot")
    monkeypatch.setenv("LIONCLAW_CHECKPOINT_BACKEND", "sqlite")
    # Not applicable on sqlite: neither run nor recorded, so it stays eligible
    # for a box that later moves to Postgres.
    assert mig.applicable() is False
    assert mig.ID not in pending_migration_ids([])
    mig.apply(lionclaw_home)  # a no-op, never a raise


def test_peer_thread_and_gc_settings_round_trip_through_the_admin_registry(lionclaw_home) -> None:
    from olano.admin.config import get_home_config, update_home_config
    from olano.config.system_settings import (
        load_checkpoint_gc_settings,
        load_peer_thread_settings,
    )

    (lionclaw_home / "config.yaml").write_text("foo: bar\n", encoding="utf-8")
    update_home_config({"peer_threads": {"max_age_days": 10, "max_turns": 50, "rotate": False}})
    update_home_config({"checkpoint_gc": {"interval_hours": 12, "enabled": False}})
    pt = load_peer_thread_settings()
    assert (pt.rotate, pt.max_age_days, pt.max_turns) == (False, 10, 50)
    gc = load_checkpoint_gc_settings()
    assert (gc.enabled, gc.interval_hours, gc.keep_min) == (False, 12, 3)
    report = get_home_config()["blocks"]
    assert report["peer_threads"]["max_age_days"] == 10
    assert report["checkpoint_gc"]["interval_hours"] == 12
    # Unrelated keys and the untouched fields survive the merge.
    import yaml

    raw = yaml.safe_load((lionclaw_home / "config.yaml").read_text(encoding="utf-8"))
    assert raw["foo"] == "bar"
    assert raw["checkpoint_gc"]["max_minutes"] == 30
