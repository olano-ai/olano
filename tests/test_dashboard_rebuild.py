"""Tests for deployment-aware dashboard rebuild (olano/update/dashboard.py).

Cover the change-detection that decides whether an update rebuilds the SPA, and
the per-deployment routing that either builds in-process or surfaces the right
host command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.platform_control.detect import InstallMode, SupervisionMode
from olano.update import dashboard as db


def _source_checkout(tmp_path: Path, *, scripts: tuple[str, ...] = ()) -> Path:
    """A fake git checkout with dashboard sources and the requested deploy scripts."""
    (tmp_path / "olano" / "dashboard_2.0").mkdir(parents=True)
    (tmp_path / "olano" / "dashboard_2.0" / "package.json").write_text("{}")
    if scripts:
        (tmp_path / "deploy").mkdir()
        for name in scripts:
            (tmp_path / "deploy" / name).write_text("#!/usr/bin/env bash\n")
    return tmp_path


# ── dashboard_changed ────────────────────────────────────────────────────────


class TestDashboardChanged:
    def test_no_old_head_is_conservative(self, tmp_path: Path) -> None:
        # Can't diff → assume it changed (rebuild rather than ship stale).
        assert db.dashboard_changed(tmp_path, "") is True

    def test_touched_dashboard_sources(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            db, "_run", lambda *a, **k: (0, "olano/dashboard_2.0/src/App.tsx\nREADME.md")
        )
        assert db.dashboard_changed(tmp_path, "abc123") is True

    def test_touched_assets(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(db, "_run", lambda *a, **k: (0, "assets/theme1/logo.png"))
        assert db.dashboard_changed(tmp_path, "abc123") is True

    def test_python_only_change_skips(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(db, "_run", lambda *a, **k: (0, "olano/core/runner.py\ndocs/x.md"))
        assert db.dashboard_changed(tmp_path, "abc123") is False

    def test_git_error_is_conservative(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(db, "_run", lambda *a, **k: (128, "fatal: bad object"))
        assert db.dashboard_changed(tmp_path, "abc123") is True


# ── rebuild_dashboard routing ────────────────────────────────────────────────


class TestRebuildRouting:
    def test_docker_surfaces_host_command(self, tmp_path: Path) -> None:
        out = db.rebuild_dashboard(tmp_path, InstallMode.DOCKER, SupervisionMode.DOCKER)
        assert out.attempted is False
        assert out.built is False
        assert out.host_command and "docker compose" in out.host_command
        assert out.ok is False

    def test_container_supervision_even_if_source(self, tmp_path: Path) -> None:
        # A git-init'd container detects as SOURCE install but DOCKER supervision;
        # the backend still can't rebuild the separate web container.
        out = db.rebuild_dashboard(tmp_path, InstallMode.SOURCE, SupervisionMode.DOCKER)
        assert out.host_command and "docker compose" in out.host_command

    def test_uv_tool_ships_with_package(self, tmp_path: Path) -> None:
        out = db.rebuild_dashboard(tmp_path, InstallMode.UV_TOOL, SupervisionMode.FOREGROUND)
        assert out.attempted is False
        assert out.host_command is None
        assert out.ok is True

    def test_no_checkout_nothing_to_do(self) -> None:
        out = db.rebuild_dashboard(None, InstallMode.SOURCE, SupervisionMode.FOREGROUND)
        assert out.attempted is False
        assert out.host_command is None

    def test_npm_missing_points_at_host(self, tmp_path: Path, monkeypatch) -> None:
        root = _source_checkout(tmp_path, scripts=("build-dashboard.sh",))
        monkeypatch.setattr(db.shutil, "which", lambda _: None)
        out = db.rebuild_dashboard(root, InstallMode.SOURCE, SupervisionMode.FOREGROUND)
        assert out.attempted is False
        assert out.host_command == "bash deploy/build-dashboard.sh"

    def test_foreground_builds_in_process(self, tmp_path: Path, monkeypatch) -> None:
        root = _source_checkout(tmp_path, scripts=("build-dashboard.sh",))
        monkeypatch.setattr(db.shutil, "which", lambda _: "/usr/bin/npm")
        calls: list[list[str]] = []

        def _fake_run(cmd, **kw):
            calls.append(cmd)
            return 0, "✓ Built"

        monkeypatch.setattr(db, "_run", _fake_run)
        out = db.rebuild_dashboard(root, InstallMode.SOURCE, SupervisionMode.FOREGROUND)
        assert out.built is True
        assert out.host_command is None
        assert calls and calls[0][0] == "bash" and calls[0][-1].endswith("build-dashboard.sh")

    def test_systemd_uses_manual_host_script(self, tmp_path: Path, monkeypatch) -> None:
        root = _source_checkout(tmp_path, scripts=("tc-manual-host.sh", "build-dashboard.sh"))
        monkeypatch.setattr(db.shutil, "which", lambda _: "/usr/bin/npm")
        calls: list[list[str]] = []

        def _fake_run(cmd, **kw):
            calls.append(cmd)
            return 0, "published"

        monkeypatch.setattr(db, "_run", _fake_run)
        out = db.rebuild_dashboard(root, InstallMode.SOURCE, SupervisionMode.SYSTEMD)
        assert out.built is True
        assert calls[0][-2:] == ["dashboard", "rebuild"]
        assert "tc-manual-host.sh" in calls[0][1]

    def test_systemd_publish_failure_falls_back_to_host(self, tmp_path: Path, monkeypatch) -> None:
        root = _source_checkout(tmp_path, scripts=("tc-manual-host.sh",))
        monkeypatch.setattr(db.shutil, "which", lambda _: "/usr/bin/npm")
        monkeypatch.setattr(db, "_run", lambda *a, **k: (1, "rsync: permission denied"))
        out = db.rebuild_dashboard(root, InstallMode.SOURCE, SupervisionMode.SYSTEMD)
        assert out.attempted is True
        assert out.built is False
        assert out.host_command == "bash deploy/tc-manual-host.sh dashboard rebuild"

    def test_build_failure_reports(self, tmp_path: Path, monkeypatch) -> None:
        root = _source_checkout(tmp_path, scripts=("build-dashboard.sh",))
        monkeypatch.setattr(db.shutil, "which", lambda _: "/usr/bin/npm")
        monkeypatch.setattr(db, "_run", lambda *a, **k: (2, "vite: build error"))
        out = db.rebuild_dashboard(root, InstallMode.SOURCE, SupervisionMode.FOREGROUND)
        assert out.attempted is True
        assert out.built is False
        assert out.host_command is None
        assert "failed" in out.detail


# ── freshness label ──────────────────────────────────────────────────────────


class TestDashboardFreshness:
    def test_docker_served_externally(self, tmp_path: Path) -> None:
        label = db.dashboard_freshness(tmp_path, InstallMode.DOCKER, SupervisionMode.DOCKER)
        assert "separate container" in label

    def test_uv_tool_ships(self, tmp_path: Path) -> None:
        label = db.dashboard_freshness(tmp_path, InstallMode.UV_TOOL, SupervisionMode.FOREGROUND)
        assert "package" in label

    def test_not_built_when_no_dist(self, tmp_path: Path) -> None:
        root = _source_checkout(tmp_path)
        label = db.dashboard_freshness(root, InstallMode.SOURCE, SupervisionMode.FOREGROUND)
        assert "not built" in label

    def test_up_to_date_when_marker_matches_commit(self, tmp_path: Path, monkeypatch) -> None:
        root = _source_checkout(tmp_path)
        dist = root / "olano" / "dashboard_2.0" / "dist"
        dist.mkdir()
        monkeypatch.setattr(db, "source_commit", lambda: "deadbee")
        (dist / db._BUILD_MARKER).write_text('{"commit": "deadbee", "ts": 1}')
        label = db.dashboard_freshness(root, InstallMode.SOURCE, SupervisionMode.FOREGROUND)
        assert label == "up to date"

    def test_stale_when_dashboard_changed_since_build(self, tmp_path: Path, monkeypatch) -> None:
        root = _source_checkout(tmp_path)
        dist = root / "olano" / "dashboard_2.0" / "dist"
        dist.mkdir()
        (dist / db._BUILD_MARKER).write_text('{"commit": "oldcafe", "ts": 1}')
        monkeypatch.setattr(db, "source_commit", lambda: "newbeef")
        monkeypatch.setattr(db, "_run", lambda *a, **k: (0, "olano/dashboard_2.0/src/App.tsx"))
        label = db.dashboard_freshness(root, InstallMode.SOURCE, SupervisionMode.FOREGROUND)
        assert "stale" in label


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
