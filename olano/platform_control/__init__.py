"""Platform-level infrastructure control (update / restart / shutdown).

Backs the owner-only ``/platform`` chat command and the ``/api/platform/*``
REST endpoints. :mod:`detect` figures out how this deployment is supervised
(systemd / docker / daemon / foreground) and installed (source checkout /
uv tool / docker image / binary); :mod:`controller` picks the matching
lifecycle strategy so "restart" works the same from a Telegram chat whether
the server runs under systemd on a cloud host, inside a container, or as a
local daemon.
"""

from olano.platform_control.controller import (
    Notify,
    PlatformController,
    bind_runtime,
    get_platform_controller,
    notify_runtime_ready,
    send_followup,
)
from olano.platform_control.detect import (
    InstallMode,
    SupervisionMode,
    detect_install_mode,
    detect_supervision,
)

__all__ = [
    "InstallMode",
    "Notify",
    "PlatformController",
    "SupervisionMode",
    "bind_runtime",
    "detect_install_mode",
    "detect_supervision",
    "get_platform_controller",
    "notify_runtime_ready",
    "send_followup",
]
