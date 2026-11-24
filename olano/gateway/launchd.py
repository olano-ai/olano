"""macOS launchd plist writer for the Olano gateway daemon."""

from __future__ import annotations

import subprocess
from pathlib import Path

_LABEL = "com.lionclaw.gateway"


def _plist_dir(base_dir: Path | None = None) -> Path:
    if base_dir:
        return base_dir
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(base_dir: Path | None = None) -> Path:
    return _plist_dir(base_dir) / f"{_LABEL}.plist"


def _build_plist(home: Path, binary: str, log_dir: Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
        <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OLANO_HOME</key>
        <string>{home}</string>
        <key>LIONCLAW_HOME</key>
        <string>{home}</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir / "gateway.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / "gateway.err.log"}</string>
</dict>
</plist>
"""


def install_launchd(home: Path, *, plist_base_dir: Path | None = None) -> Path:
    from olano.gateway import _lionclaw_binary

    binary = _lionclaw_binary()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_dir = _plist_dir(plist_base_dir)
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{_LABEL}.plist"
    plist_path.write_text(_build_plist(home, binary, log_dir), encoding="utf-8")
    return plist_path


def gateway_status() -> str:
    try:
        result = subprocess.run(
            ["launchctl", "list", _LABEL],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return "running"
        return "stopped"
    except Exception:
        return "unknown"
