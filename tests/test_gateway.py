"""Tests for olano/gateway/ platform unit file generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from olano.gateway.docker_sidecar import install_sidecar
from olano.gateway.launchd import install_launchd
from olano.gateway.systemd import install_systemd

# ─────────────────────────────────────────────────────────────────
# launchd (macOS)
# ─────────────────────────────────────────────────────────────────


def test_install_launchd_writes_plist(tmp_path: Path):
    home = tmp_path / "home"
    plist_base = tmp_path / "LaunchAgents"
    with patch("olano.gateway._lionclaw_binary", return_value="/usr/local/bin/olano"):
        plist_path = install_launchd(home, plist_base_dir=plist_base)
    assert plist_path.exists()
    content = plist_path.read_text()
    assert "com.lionclaw.gateway" in content
    assert "KeepAlive" in content
    assert "<true/>" in content
    assert str(home) in content


def test_install_launchd_creates_log_dir(tmp_path: Path):
    home = tmp_path / "home"
    plist_base = tmp_path / "plist"
    with patch("olano.gateway._lionclaw_binary", return_value="/usr/bin/olano"):
        install_launchd(home, plist_base_dir=plist_base)
    assert (home / "logs").is_dir()


# ─────────────────────────────────────────────────────────────────
# systemd (Linux)
# ─────────────────────────────────────────────────────────────────


def test_install_systemd_writes_unit(tmp_path: Path):
    home = tmp_path / "home"
    unit_base = tmp_path / "systemd"
    with patch("olano.gateway._lionclaw_binary", return_value="/usr/local/bin/olano"):
        unit_path = install_systemd(home, unit_base_dir=unit_base)
    assert unit_path.exists()
    content = unit_path.read_text()
    assert "Restart=always" in content
    assert str(home) in content
    assert "[Service]" in content


def test_install_systemd_creates_log_dir(tmp_path: Path):
    home = tmp_path / "home"
    unit_base = tmp_path / "units"
    with patch("olano.gateway._lionclaw_binary", return_value="/usr/bin/olano"):
        install_systemd(home, unit_base_dir=unit_base)
    assert (home / "logs").is_dir()


# ─────────────────────────────────────────────────────────────────
# docker sidecar
# ─────────────────────────────────────────────────────────────────


def test_install_docker_sidecar_writes_compose_file(tmp_path: Path):
    home = tmp_path / "home"
    out_dir = tmp_path / "out"
    with patch("olano.gateway._lionclaw_binary", return_value="/usr/local/bin/olano"):
        compose_path = install_sidecar(home, output_dir=out_dir)
    assert compose_path.exists()
    content = compose_path.read_text()
    assert "olano-gateway" in content
    assert "restart: always" in content
    assert str(home) in content


# ─────────────────────────────────────────────────────────────────
# gateway status mocking
# ─────────────────────────────────────────────────────────────────


def test_launchd_status_running(tmp_path: Path):
    import subprocess

    from olano.gateway.launchd import gateway_status

    mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_result):
        assert gateway_status() == "running"


def test_launchd_status_stopped(tmp_path: Path):
    import subprocess

    from olano.gateway.launchd import gateway_status

    mock_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_result):
        assert gateway_status() == "stopped"


def test_systemd_status_active(tmp_path: Path):
    import subprocess

    from olano.gateway.systemd import gateway_status

    mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="active\n", stderr="")
    with patch("subprocess.run", return_value=mock_result):
        assert gateway_status() == "running"


def test_systemd_status_inactive(tmp_path: Path):
    import subprocess

    from olano.gateway.systemd import gateway_status

    mock_result = subprocess.CompletedProcess(args=[], returncode=3, stdout="inactive\n", stderr="")
    with patch("subprocess.run", return_value=mock_result):
        assert gateway_status() == "stopped"


# ─────────────────────────────────────────────────────────────────
# install() dispatch
# ─────────────────────────────────────────────────────────────────


def test_install_dispatches_to_launchd(tmp_path: Path):
    from olano.gateway import install

    with patch(
        "olano.gateway.launchd.install_launchd", return_value=tmp_path / "plist"
    ) as mock_install:
        install(tmp_path, platform_override="macos")
    mock_install.assert_called_once_with(tmp_path)


def test_install_dispatches_to_systemd(tmp_path: Path):
    from olano.gateway import install

    with patch(
        "olano.gateway.systemd.install_systemd", return_value=tmp_path / "unit"
    ) as mock_install:
        install(tmp_path, platform_override="linux")
    mock_install.assert_called_once_with(tmp_path)


def test_install_dispatches_to_docker(tmp_path: Path):
    from olano.gateway import install

    with patch(
        "olano.gateway.docker_sidecar.install_sidecar", return_value=tmp_path / "compose"
    ) as mock_install:
        install(tmp_path, platform_override="docker")
    mock_install.assert_called_once_with(tmp_path)
