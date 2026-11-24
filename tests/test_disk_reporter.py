"""Cloud disk reporter: payload assembly, docker df parsing, POST + stamping.

No network and no docker anywhere — httpx.MockTransport carries the POSTs and
``run_fn`` fakes carry ``docker system df`` (same convention as
test_disk_monitor.py).
"""

from __future__ import annotations

import json
import subprocess

import httpx

from olano.deployment import disk_reporter
from olano.deployment.sources import CloudConfig
from olano.runtime import disk_scan


def test_parse_docker_size():
    assert disk_reporter.parse_docker_size("0B") == 0
    assert disk_reporter.parse_docker_size("356.8MB") == 356_800_000
    assert disk_reporter.parse_docker_size("3.16GB") == 3_160_000_000
    assert disk_reporter.parse_docker_size("3.1GB (73%)") == 3_100_000_000
    assert disk_reporter.parse_docker_size("1.5kB") == 1500
    assert disk_reporter.parse_docker_size("N/A") is None


def _df_stdout() -> str:
    rows = [
        {
            "Type": "Images",
            "TotalCount": 9,
            "Active": 4,
            "Size": "12.4GB",
            "Reclaimable": "7.9GB (63%)",
        },
        {
            "Type": "Containers",
            "TotalCount": 5,
            "Active": 4,
            "Size": "356.8MB",
            "Reclaimable": "0B (0%)",
        },
        {
            "Type": "Local Volumes",
            "TotalCount": 3,
            "Active": 3,
            "Size": "1.2GB",
            "Reclaimable": "0B (0%)",
        },
        {"Type": "Build Cache", "TotalCount": 0, "Active": 0, "Size": "0B", "Reclaimable": "0B"},
    ]
    return "\n".join(json.dumps(r) for r in rows)


def test_collect_docker_df_parses_totals(monkeypatch):
    monkeypatch.setattr("olano.runtime.disk_monitor.docker_daemon_reachable", lambda: True)

    def run_fn(argv, **_kw):
        assert argv == ["docker", "system", "df", "--format", "{{json .}}"]
        return subprocess.CompletedProcess(argv, 0, stdout=_df_stdout(), stderr="")

    out = disk_reporter.collect_docker_df(run_fn=run_fn, which_fn=lambda _b: "/usr/bin/docker")
    assert out is not None
    assert out["images"]["size_bytes"] == 12_400_000_000
    assert out["images"]["reclaimable_bytes"] == 7_900_000_000
    assert out["containers"]["count"] == 5
    assert out["build_cache"]["size_bytes"] == 0


def test_collect_docker_df_none_without_docker(monkeypatch):
    monkeypatch.setattr("olano.runtime.disk_monitor.docker_daemon_reachable", lambda: False)
    assert disk_reporter.collect_docker_df(which_fn=lambda _b: "/usr/bin/docker") is None
    assert disk_reporter.collect_docker_df(which_fn=lambda _b: None) is None


def test_build_disk_metrics_includes_fs_and_scan(lionclaw_home, monkeypatch):
    monkeypatch.setattr("olano.runtime.disk_monitor.docker_daemon_reachable", lambda: False)
    (lionclaw_home / "sessions").mkdir(parents=True, exist_ok=True)
    (lionclaw_home / "sessions" / "big.jsonl").write_bytes(b"x" * 500)
    result = disk_scan.scan_directory_sizes(lionclaw_home, throttle_ms=0)
    disk_scan.save_snapshot(lionclaw_home, result)

    payload = disk_reporter.build_disk_metrics(lionclaw_home)
    assert payload["fs"]["total_bytes"] > 0
    assert payload["scan"]["total_bytes"] == result.total_bytes
    assert all(e["depth"] <= disk_reporter.MAX_SCAN_DEPTH for e in payload["scan"]["entries"])
    assert "docker" not in payload  # unreachable daemon → field omitted, not null


def test_report_posts_metrics_and_stamps_state(lionclaw_home, monkeypatch):
    monkeypatch.setattr("olano.runtime.disk_monitor.docker_daemon_reachable", lambda: False)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/health"
        assert request.headers["Authorization"] == "Bearer olano_dep_test"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    cfg = CloudConfig(url="https://cloud.test", token="olano_dep_test")
    ok = disk_reporter.report_disk_metrics(
        lionclaw_home, config=cfg, transport=httpx.MockTransport(handler)
    )
    assert ok is True
    assert "disk" in seen[0]["metrics"] and "fs" in seen[0]["metrics"]["disk"]
    assert disk_reporter.load_state(lionclaw_home).get("last_reported_at")
    assert disk_reporter.report_due(lionclaw_home) is False  # just reported


def test_report_carries_version_identity_when_baked(lionclaw_home, monkeypatch):
    """The daily report self-heals the control plane's version columns:
    runtime_version (legacy) AND build_sha (canonical reported_commit).
    Both omitted when unknown — never a placeholder."""
    monkeypatch.setattr("olano.runtime.disk_monitor.docker_daemon_reachable", lambda: False)
    monkeypatch.setenv("OLANO_BUILD_SHA", "abc123def4567890")
    monkeypatch.setattr("olano.deployment.version._package_version", lambda: "0.10.6")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    cfg = CloudConfig(url="https://cloud.test", token="olano_dep_test")
    assert disk_reporter.report_disk_metrics(
        lionclaw_home, config=cfg, transport=httpx.MockTransport(handler)
    )
    assert seen[0]["runtime_version"] == "git-abc123def4567890"
    assert seen[0]["build_sha"] == "abc123def4567890"
    # The release name rides along too (deployments.reported_version).
    assert seen[0]["package_version"] == "0.10.6"


def test_failed_post_leaves_report_due(lionclaw_home, monkeypatch):
    monkeypatch.setattr("olano.runtime.disk_monitor.docker_daemon_reachable", lambda: False)
    cfg = CloudConfig(url="https://cloud.test", token="olano_dep_test")
    transport = httpx.MockTransport(lambda _req: httpx.Response(500))
    assert (
        disk_reporter.report_disk_metrics(lionclaw_home, config=cfg, transport=transport) is False
    )
    assert disk_reporter.load_state(lionclaw_home) == {}
    assert disk_reporter.report_due(lionclaw_home) is True


def test_no_config_is_a_quiet_no_send(lionclaw_home):
    # No deployment token in the test env → cloud_settings_config() is None.
    assert disk_reporter.report_disk_metrics(lionclaw_home) is False
