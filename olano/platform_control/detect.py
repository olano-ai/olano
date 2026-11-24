"""Deployment-mode detection for platform infrastructure commands.

Two orthogonal facts drive every platform-control strategy:

- **Supervision** — who brings the server back after it exits. Under
  systemd (``Restart=always``) or docker (``restart: unless-stopped``) a
  clean exit *is* a restart; under the ``olano server`` daemon or a
  foreground ``olano serve`` the process must re-exec itself.
- **Install mode** — how the code got onto the box, which decides how
  "update" works: ``git pull`` for a source checkout, ``uv tool upgrade``
  for a PyPI install, an image rebuild for baked docker, nothing yet for
  the future compiled binary.
"""

from __future__ import annotations

import os
import subprocess
import sys
from enum import Enum
from pathlib import Path


class SupervisionMode(str, Enum):
    """Who supervises (and can respawn) the runtime process."""

    DOCKER = "docker"
    SYSTEMD = "systemd"
    DAEMON = "daemon"
    FOREGROUND = "foreground"


class InstallMode(str, Enum):
    """How olano was installed on this host."""

    SOURCE = "source"
    UV_TOOL = "uv-tool"
    DOCKER = "docker"
    BINARY = "binary"


# ``LIONCLAW_INSTALL_MODE`` / ``.install.json`` historically used "local" for
# the uv-tool (PyPI) install; accept every spelling we've shipped.
_MODE_ALIASES: dict[str, InstallMode] = {
    "source": InstallMode.SOURCE,
    "git": InstallMode.SOURCE,
    "docker": InstallMode.DOCKER,
    "local": InstallMode.UV_TOOL,
    "uv-tool": InstallMode.UV_TOOL,
    "uv_tool": InstallMode.UV_TOOL,
    "binary": InstallMode.BINARY,
}


def repo_root() -> Path | None:
    """Root of the git checkout when olano runs from source, else None.

    True both for a dev/TC-host ``uv run`` checkout and for an editable
    install inside a docker container after the fleet tooling's ``update-code.sh`` has
    initialized git there — in both cases "update" means ``git pull``.
    """
    try:
        import olano

        root = Path(olano.__file__).resolve().parents[1]
    except Exception:
        return None
    return root if (root / ".git").exists() else None


def detect_supervision() -> SupervisionMode:
    from olano.security.sharing import is_running_in_container

    if is_running_in_container():
        return SupervisionMode.DOCKER
    # systemd stamps INVOCATION_ID on every unit it starts.
    if os.environ.get("INVOCATION_ID"):
        return SupervisionMode.SYSTEMD
    if os.environ.get("LIONCLAW_DAEMON") == "1":
        return SupervisionMode.DAEMON
    try:
        from olano.runtime.daemon import read_pid

        if read_pid() == os.getpid():
            return SupervisionMode.DAEMON
    except Exception:
        pass
    return SupervisionMode.FOREGROUND


def detect_install_mode(home: Path | None = None) -> InstallMode:
    env = os.environ.get("LIONCLAW_INSTALL_MODE", "").strip().lower()
    if env in _MODE_ALIASES:
        return _MODE_ALIASES[env]
    if getattr(sys, "frozen", False):
        return InstallMode.BINARY
    if repo_root() is not None:
        return InstallMode.SOURCE
    from olano.security.sharing import is_running_in_container

    if is_running_in_container():
        return InstallMode.DOCKER
    try:
        from olano.config.home import get_olano_home
        from olano.update.install_state import read_install_state

        recorded = read_install_state(home or get_olano_home()).mode
        if recorded in _MODE_ALIASES:
            return _MODE_ALIASES[recorded]
    except Exception:
        pass
    return InstallMode.UV_TOOL


def systemd_unit_name() -> str | None:
    """The systemd unit this process runs under, if determinable.

    ``deploy/tc-manual-host.sh`` exports ``LIONCLAW_SYSTEMD_UNIT`` in the
    unit file; otherwise fall back to parsing the cgroup path (e.g.
    ``0::/system.slice/olano.service``).
    """
    unit = os.environ.get("LIONCLAW_SYSTEMD_UNIT", "").strip()
    if unit:
        return unit
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            for part in reversed(line.split("/")):
                if part.endswith(".service"):
                    return part
    except Exception:
        pass
    return None


def current_version() -> str:
    try:
        from importlib.metadata import version

        return version("olano")
    except Exception:
        return "unknown"


def source_commit() -> str | None:
    """Short git SHA when running from a source checkout, else None."""
    root = repo_root()
    if root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        sha = result.stdout.strip()
        return sha or None
    except Exception:
        return None
