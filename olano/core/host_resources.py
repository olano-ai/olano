"""Host CPU/RAM detection and the sandbox resource-limit resolver.

The Docker sandbox used to run every agent at a hardcoded ``--memory=512m
--cpus=0.5``, which is the right answer only on the smallest box olano ships
on. This module answers two questions instead:

1. **How big is the host?** ``docker info`` over the mounted socket is the
   authority (it reports the *daemon's* view, i.e. the real machine, not this
   container's cgroup). ``/proc/meminfo`` + ``os.cpu_count()`` are the fallback
   and happen to be host-true here as well: procfs is not namespaced, and the
   runtime container carries a ``mem_limit`` but no ``--cpus`` pin. Both are
   memoised, because ``docker info`` is a daemon round-trip and the config API
   is polled.

2. **What limits does this agent's sandbox get?** One resolution rule, applied
   per field so an agent that sets only ``memory`` still inherits the CPU
   answer: **per-agent override → environment → config.yaml → a fraction of
   the host → the historical 512m/0.5 floor**. Environment beats config.yaml
   to match ``config/home.py:_resolve_backend``, the repo's existing
   precedence for the same shape of question.

The fractions are deliberately *ceilings, not reservations*: docker enforces
them only when a container actually asks for the memory, so sizing every
sandbox at a quarter of RAM does not reserve anything, and idle agents cost
nothing. Several simultaneously-busy agents can therefore overcommit the box —
that is the accepted trade (the alternative, dividing by the agent count,
starves the common case of one agent working at a time). The guardrails
underneath are the runtime container's own ``mem_limit``, earlyoom, and
per-container OOM-kill.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypeVar

from olano.config.backend_config import (
    format_mib,
    normalize_sandbox_cpus,
    normalize_sandbox_memory,
    parse_mem_size,
)

if TYPE_CHECKING:
    from olano.config.system_settings import SandboxSettings

logger = logging.getLogger(__name__)

# Fractions of the host each sandbox may use. Ceilings, not reservations.
SANDBOX_MEMORY_FRACTION = 0.25
SANDBOX_CPU_FRACTION = 0.5
# The historical caps. They stay the floor (a 1 GB box keeps working exactly as
# it did) and the answer when detection fails entirely.
FALLBACK_MEMORY = "512m"
FALLBACK_CPUS = 0.5
_FALLBACK_MEMORY_BYTES = 512 * 1024**2

ENV_MEMORY = "LIONCLAW_SANDBOX_MEMORY"
ENV_CPUS = "LIONCLAW_SANDBOX_CPUS"

# `docker info` is a daemon round-trip and /api/system/config is polled; the
# host's size does not change under a running box.
DEFAULT_TTL_SECONDS = 300.0
_DOCKER_INFO_TIMEOUT = 10


class HostResources(NamedTuple):
    """What the box has, as far as this process can tell.

    Either field is ``None`` when that fact could not be established; callers
    fall back per field rather than discarding the half that worked.
    """

    ncpu: int | None
    mem_bytes: int | None
    source: str  # "docker_info" | "proc" | "unavailable"


class SandboxLimits(NamedTuple):
    """Resolved ceilings plus which layer decided each one.

    The sources are what the dashboard shows and what the boot log records —
    "why is this sandbox 2 CPUs" must be answerable without a debugger.
    """

    memory: str
    cpus: float
    memory_source: str  # "agent" | "env" | "config" | "auto" | "fallback"
    cpus_source: str
    host: HostResources


# ── host detection ───────────────────────────────────────────────────────────

_CACHE_LOCK = threading.Lock()
_CACHE: tuple[float, HostResources] | None = None


def invalidate_host_resources_cache() -> None:
    """Drop the memoised host facts (tests, and any explicit re-probe)."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def _from_docker_info(
    run_fn: Callable[..., subprocess.CompletedProcess[str]],
    which_fn: Callable[[str], str | None],
    docker_fn: Callable[[], bool] | None,
) -> HostResources | None:
    """Host CPU/RAM per the docker daemon, or None when it cannot be asked."""
    from olano.runtime.disk_monitor import docker_daemon_reachable

    if which_fn("docker") is None:
        return None
    if not (docker_fn or docker_daemon_reachable)():
        return None
    try:
        proc = run_fn(
            ["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_INFO_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("host resources: docker info failed: %s", e)
        return None
    if proc.returncode != 0:
        logger.debug("host resources: docker info exit %s", proc.returncode)
        return None

    parts = (proc.stdout or "").split()
    if len(parts) != 2:
        return None
    try:
        ncpu, mem_bytes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    # A daemon that reports 0 knows less than /proc does; treat it as unknown
    # so the fallback gets its turn instead of sizing against zero.
    resolved = HostResources(
        ncpu=ncpu if ncpu > 0 else None,
        mem_bytes=mem_bytes if mem_bytes > 0 else None,
        source="docker_info",
    )
    return resolved if (resolved.ncpu or resolved.mem_bytes) else None


def _read_meminfo_total(meminfo_path: str | Path) -> int | None:
    """``MemTotal`` in bytes from procfs — host-wide even inside a container."""
    try:
        raw = Path(meminfo_path).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        if not line.startswith("MemTotal:"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                return int(parts[1]) * 1024  # procfs reports kB
            except ValueError:
                return None
    return None


def detect_host_resources(
    *,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which_fn: Callable[[str], str | None] = shutil.which,
    docker_fn: Callable[[], bool] | None = None,
    meminfo_path: str | Path = "/proc/meminfo",
    cpu_count_fn: Callable[[], int | None] = os.cpu_count,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now_fn: Callable[[], float] = time.monotonic,
    use_cache: bool = True,
) -> HostResources:
    """The host's CPU count and total RAM, memoised for ``ttl_seconds``.

    Never raises and never blocks longer than the docker timeout. Degradation is
    **per field**: a docker answer that supplies only one of the two is topped up
    from procfs rather than leaving the other unknown, and only an all-failure
    returns ``(None, None, "unavailable")`` — which leaves the caller on the
    historical caps.
    """
    global _CACHE
    stamp = now_fn()
    if use_cache:
        with _CACHE_LOCK:
            if _CACHE is not None and (stamp - _CACHE[0]) < max(0.0, ttl_seconds):
                return _CACHE[1]

    docker = _from_docker_info(run_fn, which_fn, docker_fn)
    ncpu = docker.ncpu if docker else None
    mem_bytes = docker.mem_bytes if docker else None

    if ncpu is None or mem_bytes is None:
        if ncpu is None:
            probed = cpu_count_fn()
            ncpu = probed if (probed or 0) > 0 else None
        if mem_bytes is None:
            probed_mem = _read_meminfo_total(meminfo_path)
            mem_bytes = probed_mem if (probed_mem or 0) > 0 else None

    if ncpu is None and mem_bytes is None:
        source = "unavailable"
    elif docker and (docker.ncpu is not None or docker.mem_bytes is not None):
        # Name the authority even when procfs filled a gap: it is what the
        # dashboard shows, and "docker_info" is the answer that came from the
        # real machine rather than from this container's view of it.
        source = "docker_info"
    else:
        source = "proc"

    resolved = HostResources(ncpu=ncpu, mem_bytes=mem_bytes, source=source)
    if use_cache:
        with _CACHE_LOCK:
            _CACHE = (stamp, resolved)
    return resolved


# ── resolution ───────────────────────────────────────────────────────────────


def auto_memory(host: HostResources) -> str | None:
    """A quarter of host RAM, never below the historical floor."""
    if not host.mem_bytes:
        return None
    return format_mib(max(_FALLBACK_MEMORY_BYTES, int(host.mem_bytes * SANDBOX_MEMORY_FRACTION)))


def auto_cpus(host: HostResources) -> float | None:
    """Half the host's cores, never below the historical floor."""
    if not host.ncpu:
        return None
    # Two decimals keeps the value stable across boots — it feeds the container
    # signature, and a drifting float would recreate containers for nothing.
    return max(FALLBACK_CPUS, round(host.ncpu * SANDBOX_CPU_FRACTION, 2))


_T = TypeVar("_T")


def _env_value(env: Mapping[str, str], key: str, normalize: Callable[[str], _T]) -> _T | None:
    """One env override, or None when unset or unusable.

    A bad value warns and is ignored rather than failing the agent's boot: the
    variable is deployment-wide, so raising here would take every agent offline
    over one typo in ``.env``.
    """
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        return normalize(raw)
    except ValueError as e:
        logger.warning("%s=%r ignored: %s", key, raw, e)
        return None


def _first_of(candidates: list[tuple[_T | None, str]]) -> tuple[_T, str]:
    """The first candidate that isn't ``None``, with the label that named it.

    The last entry is the floor and must never be ``None`` — the caller always
    ends the chain with a literal, so this cannot fall through.
    """
    for value, source in candidates:
        if value is not None:
            return value, source
    raise AssertionError("resolution chain ended without a fallback value")


def resolve_sandbox_limits(
    agent_memory: str | None = None,
    agent_cpus: float | None = None,
    *,
    settings: SandboxSettings | None = None,
    env: Mapping[str, str] | None = None,
    host: HostResources | None = None,
) -> SandboxLimits:
    """Resolve one sandbox's ceilings: agent → env → config.yaml → auto → floor.

    Each field resolves independently, so a per-agent ``memory`` override does
    not drag that agent's CPU cap along with it.
    """
    env = os.environ if env is None else env
    if settings is None:
        from olano.config.system_settings import load_sandbox_settings

        settings = load_sandbox_settings()
    if host is None:
        host = detect_host_resources()

    # The chain is written as an ordered list rather than a stack of ifs so the
    # precedence — the whole point of this function — is readable at a glance.
    # Candidates are evaluated eagerly, which is deliberate: an unusable env var
    # then warns even on a box where every agent happens to override it, and the
    # rest are a dict lookup and some arithmetic.
    memory, memory_source = _first_of(
        [
            (agent_memory, "agent"),
            (_env_value(env, ENV_MEMORY, normalize_sandbox_memory), "env"),
            (settings.memory, "config"),
            (auto_memory(host), "auto"),
            (FALLBACK_MEMORY, "fallback"),
        ]
    )
    cpus, cpus_source = _first_of(
        [
            (agent_cpus, "agent"),
            (_env_value(env, ENV_CPUS, normalize_sandbox_cpus), "env"),
            (settings.cpus, "config"),
            (auto_cpus(host), "auto"),
            (FALLBACK_CPUS, "fallback"),
        ]
    )

    return SandboxLimits(
        memory=memory,
        cpus=cpus,
        memory_source=memory_source,
        cpus_source=cpus_source,
        host=host,
    )


def describe_bytes(num_bytes: int | None) -> str:
    """Short human size for logs and the dashboard summary line."""
    if not num_bytes:
        return "unknown"
    gib = num_bytes / 1024**3
    return f"{gib:.1f} GB" if gib >= 1 else f"{num_bytes // 1024**2} MB"


def memory_bytes(limit: str) -> int | None:
    """Bytes for a resolved memory limit (re-exported for callers/tests)."""
    return parse_mem_size(limit)
