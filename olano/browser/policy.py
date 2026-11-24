"""Whether agents get a browser, and which one: the single answer every surface asks.

``config.yaml → browser.mode`` is what the operator *asked for*. What agents
actually get is that request after three gates, in this order:

1. **The deployment entitlement.** ``features.browser`` gates every mode and
   ``features.browser_live`` the live one (``deployment/schema.KNOWN_FEATURES``,
   missing ⇒ enabled). A plan without live keeps headless.
2. **The hardware.** A local browser on a box too small for it would push the
   runtime into the OOM killer, which takes every agent down with it, so the
   gate refuses rather than warns: below :data:`HEADLESS_MIN_MEM_BYTES` of RAM
   (a 1 GB machine) there is no local browser at all, and live needs
   :data:`LIVE_MIN_CPUS` CPUs and :data:`LIVE_MIN_MEM_BYTES`. A request the box
   cannot carry degrades one step (live → headless → off) with the reason
   attached. ``provider: remote`` costs this machine no browser memory and
   skips this gate.
3. **Docker**, for live with the local provider: the live browser is a
   container, so a box with no docker CLI degrades to headless.

``browser_availability()`` returns the effective mode with a machine code and a
human reason; ``browser_tools_allowed()`` is the predicate tool binding asks.
``enable_refusal()`` is the stricter check for the moment someone turns the
browser on: it also wants enough memory *free right now* for one session,
because enabling a browser on a box already near its ceiling only moves the
failure to the first page load.

Nothing here raises: an unreadable setting is ``off``, an undetectable host is
assumed big enough (the per-session free-memory check at open time still
stands guard).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

GIB = 1024**3
MIB = 1024**2

# Total RAM. A "2 GB" machine reports ~1.9 GiB once the kernel takes its share,
# a "4 GB" one ~3.8 GiB, so the floors sit below the nominal sizes on purpose.
HEADLESS_MIN_MEM_BYTES = int(1.5 * GIB)
LIVE_MIN_MEM_BYTES = int(3.2 * GIB)
LIVE_MIN_CPUS = 2

# Free memory one more session needs, measured on Chromium 1194 with two heavy
# tabs (PSS): ~250 MB headless + the Playwright driver; ~360 MB for the full
# browser plus the display server and the exec relay. Rounded up.
HEADLESS_SESSION_BYTES = 350 * MIB
LIVE_SESSION_BYTES = 700 * MIB

# Every tool that drives or launches a browser. Kept here, not derived from the
# toolkit, so the gate cannot be widened by importing the (heavy) module.
BROWSER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_fill",
        "browser_press_key",
        "browser_extract",
        "browser_screenshot",
        "browser_record",
        "browser_request_user",
        "browser_close",
        "browser_get_auto_approvals",
        "browser_set_auto_approvals",
        "browser_navigate_and_screenshot",
        "page_screenshot",
    }
)


# Approvals only a person may grant, every time (``runner._always_asks_human``):
# every browser tool, plus the fleet tool that changes an agent's browser
# auto-approval rules (it lives in the admin toolkit, so it is not a browser
# tool and must not vanish when the browser is off).
ALWAYS_ASK_TOOL_NAMES: frozenset[str] = BROWSER_TOOL_NAMES | {"olano_set_browser_auto_approvals"}


@dataclass(frozen=True)
class BrowserAvailability:
    """The effective browser for this box, and why it differs from the request."""

    requested: str
    mode: str  # "off" | "headless" | "live"
    provider: str  # "local" | "remote"
    code: str  # "ok" | "disabled" | "not_entitled" | "live_not_entitled" | ...
    reason: str = ""
    max_sessions: int = 1
    idle_timeout_minutes: int = 10
    timezone: str = ""

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def to_dict(self) -> dict:
        return {**asdict(self), "enabled": self.enabled}


def _feature(name: str) -> bool:
    try:
        from olano.deployment import enforcement

        return bool(enforcement.feature_enabled(name))
    except Exception:  # pragma: no cover - defensive: an unreadable artifact
        logger.debug("browser policy: feature %s unreadable; treating as off", name)
        return False


def _host() -> tuple[int | None, int | None]:
    try:
        from olano.core.host_resources import detect_host_resources

        host = detect_host_resources()
        return host.ncpu, host.mem_bytes
    except Exception:  # pragma: no cover - defensive
        return None, None


def _read_int(path: str) -> int | None:
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    if not text or text == "max":
        return None
    try:
        return int(text.split()[0])
    except ValueError:
        return None


def available_memory_bytes() -> int | None:
    """Memory a new browser could use right now, or ``None`` when unknowable.

    ``MemAvailable`` is the host's figure even inside a container, so a cgroup
    ceiling on this process (``memory.max`` minus ``memory.current``) is taken
    into account when one is set: the runtime can be out of room long before
    the host is.
    """
    avail: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        avail = None
    limit = _read_int("/sys/fs/cgroup/memory.max")
    current = _read_int("/sys/fs/cgroup/memory.current")
    if limit is not None and current is not None:
        headroom = max(0, limit - current)
        avail = headroom if avail is None else min(avail, headroom)
    return avail


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def session_bytes(mode: str) -> int:
    return LIVE_SESSION_BYTES if mode == "live" else HEADLESS_SESSION_BYTES


def _fmt_gb(n: int) -> str:
    return f"{n / GIB:.1f} GB"


def _hardware_mode(requested: str) -> tuple[str, str, str]:
    """(mode, code, reason) after the hardware gate, for the local provider."""
    ncpu, mem = _host()
    if mem is not None and mem < HEADLESS_MIN_MEM_BYTES:
        return (
            "off",
            "host_too_small",
            f"This machine has {_fmt_gb(mem)} of memory; a browser needs at least "
            f"{_fmt_gb(HEADLESS_MIN_MEM_BYTES)} so it cannot crowd out the agents.",
        )
    if requested != "live":
        return requested, "ok", ""
    if (mem is not None and mem < LIVE_MIN_MEM_BYTES) or (
        ncpu is not None and ncpu < LIVE_MIN_CPUS
    ):
        have = []
        if ncpu is not None:
            have.append(f"{ncpu} CPU")
        if mem is not None:
            have.append(_fmt_gb(mem))
        return (
            "headless",
            "host_too_small_for_live",
            f"The live browser needs {LIVE_MIN_CPUS} CPUs and "
            f"{_fmt_gb(LIVE_MIN_MEM_BYTES)} of memory; this machine has "
            f"{' and '.join(have) or 'less'}. Agents use the headless browser instead.",
        )
    if not _docker_available():
        return (
            "headless",
            "live_needs_docker",
            "The live browser runs in its own container and this machine has no "
            "Docker. Agents use the headless browser instead.",
        )
    return "live", "ok", ""


def browser_availability(settings: object | None = None) -> BrowserAvailability:
    """The effective browser mode. Never raises; an unreadable block is ``off``."""
    if settings is None:
        try:
            from olano.config.system_settings import load_browser_settings

            settings = load_browser_settings()
        except Exception:  # pragma: no cover - defensive
            settings = None
    requested = str(getattr(settings, "mode", "off") or "off")
    provider = str(getattr(settings, "provider", "local") or "local")
    max_sessions = int(getattr(settings, "max_sessions", 1) or 1)
    idle_timeout = int(getattr(settings, "idle_timeout", 10) or 10)
    timezone = str(getattr(settings, "timezone", "") or "")

    def _result(mode: str, code: str, reason: str = "") -> BrowserAvailability:
        return BrowserAvailability(
            requested=requested,
            mode=mode,
            provider=provider,
            code=code,
            reason=reason,
            max_sessions=max_sessions,
            idle_timeout_minutes=idle_timeout,
            timezone=timezone,
        )

    if requested == "off":
        return _result("off", "disabled")
    if not _feature("browser"):
        return _result(
            "off", "not_entitled", "Browser control is not included in this deployment's plan."
        )
    mode, code, reason = requested, "ok", ""
    if requested == "live" and not _feature("browser_live"):
        mode, code, reason = (
            "headless",
            "live_not_entitled",
            "The live browser is not included in this deployment's plan. "
            "Agents use the headless browser instead.",
        )
    if provider == "local":
        hw_mode, hw_code, hw_reason = _hardware_mode(mode)
        if hw_code != "ok":
            mode, code, reason = hw_mode, hw_code, hw_reason
    return _result(mode, code, reason)


def browser_tools_allowed() -> bool:
    """True when any browser tool may bind. The one predicate tool binding asks."""
    return browser_availability().enabled


def is_browser_tool(name: str) -> bool:
    return name in BROWSER_TOOL_NAMES


def filter_browser_tool_names(names: list[str]) -> list[str]:
    """``names`` minus the browser tools when the browser is off; else unchanged."""
    if not any(n in BROWSER_TOOL_NAMES for n in names):
        return names
    if browser_tools_allowed():
        return names
    return [n for n in names if n not in BROWSER_TOOL_NAMES]


def enable_refusal(mode: str, provider: str = "local") -> str | None:
    """Why turning the browser on to ``mode`` must be refused, or ``None``.

    Run when someone saves a non-off mode. Stricter than
    :func:`browser_availability`: a request that would only degrade there is
    refused here, so the operator learns at save time that the box cannot
    carry it instead of discovering an agent quietly got less.
    """
    if mode == "off":
        return None
    if mode not in ("headless", "live"):
        return f"Unknown browser mode {mode!r}."
    if not _feature("browser"):
        return "Browser control is not included in this deployment's plan."
    if mode == "live" and not _feature("browser_live"):
        return (
            "The live browser is not included in this deployment's plan. "
            "Choose headless, or move to a plan that includes it."
        )
    if provider == "remote":
        return None
    ncpu, mem = _host()
    if mem is not None and mem < HEADLESS_MIN_MEM_BYTES:
        return (
            f"This machine has {_fmt_gb(mem)} of memory. A browser needs at least "
            f"{_fmt_gb(HEADLESS_MIN_MEM_BYTES)}; use the remote provider instead."
        )
    if mode == "live":
        if ncpu is not None and ncpu < LIVE_MIN_CPUS:
            return f"The live browser needs {LIVE_MIN_CPUS} CPUs; this machine has {ncpu}."
        if mem is not None and mem < LIVE_MIN_MEM_BYTES:
            return (
                f"The live browser needs {_fmt_gb(LIVE_MIN_MEM_BYTES)} of memory; "
                f"this machine has {_fmt_gb(mem)}."
            )
        if not _docker_available():
            return "The live browser runs in its own container and this machine has no Docker."
    free = available_memory_bytes()
    need = session_bytes(mode)
    if free is not None and free < need:
        return (
            f"Only {free // MIB} MB of memory is free right now and one {mode} browser "
            f"needs about {need // MIB} MB. Free some memory (fewer agents, a smaller "
            "sandbox) and try again."
        )
    return None


def open_refusal(mode: str) -> str | None:
    """Why opening one more local session right now must be refused, or ``None``."""
    free = available_memory_bytes()
    need = session_bytes(mode)
    if free is not None and free < need:
        return (
            f"Not enough free memory to open a browser ({free // MIB} MB free, about "
            f"{need // MIB} MB needed). Close another agent's browser or try again later."
        )
    return None
