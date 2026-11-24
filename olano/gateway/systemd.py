"""Linux systemd user-unit writer for the Olano gateway daemon."""

from __future__ import annotations

import subprocess
from pathlib import Path

_SERVICE_NAME = "olano-gateway"


def _unit_dir(base_dir: Path | None = None) -> Path:
    if base_dir:
        return base_dir
    return Path.home() / ".config" / "systemd" / "user"


def _unit_path(base_dir: Path | None = None) -> Path:
    return _unit_dir(base_dir) / f"{_SERVICE_NAME}.service"


def _build_unit(home: Path, binary: str, log_dir: Path) -> str:
    return f"""[Unit]
Description=Olano Gateway Daemon
After=network.target

[Service]
Type=simple
ExecStart={binary} serve
Environment=OLANO_HOME={home}
Environment=LIONCLAW_HOME={home}
Restart=always
RestartSec=5
StandardOutput=append:{log_dir / "gateway.out.log"}
StandardError=append:{log_dir / "gateway.err.log"}

[Install]
WantedBy=default.target
"""


def install_systemd(home: Path, *, unit_base_dir: Path | None = None) -> Path:
    from olano.gateway import _lionclaw_binary

    binary = _lionclaw_binary()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    unit_dir = _unit_dir(unit_base_dir)
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / f"{_SERVICE_NAME}.service"
    unit_path.write_text(_build_unit(home, binary, log_dir), encoding="utf-8")
    return unit_path


def gateway_status() -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", _SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "running" if result.stdout.strip() == "active" else "stopped"
    except Exception:
        return "unknown"
