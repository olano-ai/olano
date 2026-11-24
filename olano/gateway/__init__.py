"""Gateway daemon management: install, status, start, stop, restart."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


def _detect_platform() -> str:
    """Return 'macos', 'linux', or 'docker'."""
    if os.environ.get("LIONCLAW_INSTALL_MODE") == "docker":
        return "docker"
    system = platform.system()
    if system == "Darwin":
        return "macos"
    return "linux"


def _lionclaw_binary() -> str:
    import shutil
    import sys

    return shutil.which("olano") or sys.executable


def install(home: Path, *, platform_override: str | None = None) -> Path:
    """Install the gateway daemon unit file for the current platform."""
    plat = platform_override or _detect_platform()
    if plat == "macos":
        from olano.gateway.launchd import install_launchd

        return install_launchd(home)
    elif plat == "docker":
        from olano.gateway.docker_sidecar import install_sidecar

        return install_sidecar(home)
    else:
        from olano.gateway.systemd import install_systemd

        return install_systemd(home)


def status(*, platform_override: str | None = None) -> str:
    """Return 'running', 'stopped', or 'unknown'."""
    plat = platform_override or _detect_platform()
    if plat == "macos":
        from olano.gateway.launchd import gateway_status

        return gateway_status()
    elif plat == "docker":
        return "unknown"
    else:
        from olano.gateway.systemd import gateway_status

        return gateway_status()


def start(*, platform_override: str | None = None) -> None:
    plat = platform_override or _detect_platform()
    if plat == "macos":
        subprocess.run(["launchctl", "start", "com.lionclaw.gateway"], check=False)
    elif plat == "linux":
        subprocess.run(["systemctl", "--user", "start", "olano-gateway"], check=False)


def stop(*, platform_override: str | None = None) -> None:
    plat = platform_override or _detect_platform()
    if plat == "macos":
        subprocess.run(["launchctl", "stop", "com.lionclaw.gateway"], check=False)
    elif plat == "linux":
        subprocess.run(["systemctl", "--user", "stop", "olano-gateway"], check=False)


def restart(*, platform_override: str | None = None) -> None:
    stop(platform_override=platform_override)
    start(platform_override=platform_override)
