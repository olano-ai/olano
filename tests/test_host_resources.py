"""Host detection + sandbox limit resolution.

The sandbox used to run at a hardcoded 512m/0.5 whatever the machine was, so a
16 GB box gave its agents the same room as a 1 GB one. These pin the two halves
of the fix: what the box reports about itself, and which layer's answer wins.
"""

from __future__ import annotations

import subprocess

import pytest

from olano.config.system_settings import SandboxSettings
from olano.core.host_resources import (
    ENV_CPUS,
    ENV_MEMORY,
    FALLBACK_CPUS,
    FALLBACK_MEMORY,
    HostResources,
    auto_cpus,
    auto_memory,
    detect_host_resources,
    invalidate_host_resources_cache,
    resolve_sandbox_limits,
)

GIB = 1024**3
# A 4 vCPU / 16 GB box — the shape that motivated the change.
HOST_4C_16G = HostResources(ncpu=4, mem_bytes=16 * GIB, source="docker_info")
NO_HOST = HostResources(ncpu=None, mem_bytes=None, source="unavailable")


def _run_returning(stdout: str, returncode: int = 0):
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return _run


def _run_raising(exc: Exception):
    def _run(cmd, **kwargs):
        raise exc

    return _run


def _detect(**kwargs) -> HostResources:
    """detect_host_resources with the cache off — these assert on inputs."""
    kwargs.setdefault("use_cache", False)
    return detect_host_resources(**kwargs)


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_host_resources_cache()
    yield
    invalidate_host_resources_cache()


# ── detection ────────────────────────────────────────────────────────────────


def test_docker_info_reports_the_host_not_this_container():
    """The daemon's own view is the authority: the runtime container is memory
    capped, so anything derived from its cgroup would undersize every sandbox."""
    host = _detect(
        run_fn=_run_returning("8 17179869184\n"),
        which_fn=lambda _n: "/usr/bin/docker",
        docker_fn=lambda: True,
    )
    assert host == HostResources(ncpu=8, mem_bytes=17179869184, source="docker_info")


@pytest.mark.parametrize(
    "run_fn, which_fn, docker_fn",
    [
        (_run_raising(OSError("no docker")), lambda _n: "/usr/bin/docker", lambda: True),
        (_run_returning("", returncode=1), lambda _n: "/usr/bin/docker", lambda: True),
        (_run_returning("garbage\n"), lambda _n: "/usr/bin/docker", lambda: True),
        (_run_returning("0 0\n"), lambda _n: "/usr/bin/docker", lambda: True),
        (_run_returning("8 123\n"), lambda _n: None, lambda: True),  # no docker CLI
        (_run_returning("8 123\n"), lambda _n: "/usr/bin/docker", lambda: False),  # no socket
    ],
)
def test_unusable_docker_falls_back_to_procfs(tmp_path, run_fn, which_fn, docker_fn):
    """Every docker failure mode degrades to /proc + os.cpu_count() rather than
    to nothing: procfs is not namespaced, so MemTotal is the host's either way."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16384000 kB\nMemFree:  1024 kB\n")
    host = _detect(
        run_fn=run_fn,
        which_fn=which_fn,
        docker_fn=docker_fn,
        meminfo_path=meminfo,
        cpu_count_fn=lambda: 4,
    )
    assert host.source == "proc"
    assert host.ncpu == 4
    assert host.mem_bytes == 16384000 * 1024


def test_detection_failing_everywhere_is_reported_not_guessed():
    host = _detect(
        run_fn=_run_raising(OSError("nope")),
        which_fn=lambda _n: None,
        meminfo_path="/nonexistent/meminfo",
        cpu_count_fn=lambda: None,
    )
    assert host == HostResources(ncpu=None, mem_bytes=None, source="unavailable")


def test_a_partial_docker_answer_is_topped_up_from_procfs(tmp_path):
    """A daemon that reports cores but MemTotal 0 knows less than /proc does, so
    the gap is filled rather than left unknown for the whole TTL."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16384000 kB\n")
    host = _detect(
        run_fn=_run_returning("8 0\n"),
        which_fn=lambda _n: "/usr/bin/docker",
        docker_fn=lambda: True,
        meminfo_path=meminfo,
        cpu_count_fn=lambda: 4,
    )
    assert host.ncpu == 8, "the daemon's core count is the authority"
    assert host.mem_bytes == 16384000 * 1024
    assert host.source == "docker_info"


def test_one_missing_fact_does_not_discard_the_other(tmp_path):
    host = _detect(
        run_fn=_run_raising(OSError("nope")),
        which_fn=lambda _n: None,
        meminfo_path="/nonexistent/meminfo",
        cpu_count_fn=lambda: 4,
    )
    assert host.ncpu == 4
    assert host.mem_bytes is None


def test_detection_is_memoised_and_invalidatable(tmp_path):
    """docker info is a daemon round-trip and /api/system/config is polled."""
    calls: list[list[str]] = []

    def counting_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="4 1024\n", stderr="")

    clock = [1000.0]
    kw = {
        "run_fn": counting_run,
        "which_fn": lambda _n: "/usr/bin/docker",
        "docker_fn": lambda: True,
        "now_fn": lambda: clock[0],
        "ttl_seconds": 300.0,
    }
    detect_host_resources(**kw)
    detect_host_resources(**kw)
    assert len(calls) == 1, "a second call inside the TTL must not re-probe"

    clock[0] += 301
    detect_host_resources(**kw)
    assert len(calls) == 2, "the probe must run again once the TTL lapses"

    invalidate_host_resources_cache()
    detect_host_resources(**kw)
    assert len(calls) == 3, "invalidate must force a re-probe"


# ── auto sizing ──────────────────────────────────────────────────────────────


def test_auto_sizing_is_a_fraction_of_the_host():
    assert auto_cpus(HOST_4C_16G) == 2.0
    assert auto_memory(HOST_4C_16G) == "4096m"


def test_auto_sizing_never_drops_below_the_historical_caps():
    """A 1 GB / 1 core box keeps exactly the limits it had before this existed —
    a quarter of 1 GB would be a downgrade, not a fix."""
    tiny = HostResources(ncpu=1, mem_bytes=GIB, source="proc")
    assert auto_cpus(tiny) == FALLBACK_CPUS
    assert auto_memory(tiny) == FALLBACK_MEMORY


def test_auto_sizing_declines_when_the_host_is_unknown():
    assert auto_cpus(NO_HOST) is None
    assert auto_memory(NO_HOST) is None


# ── resolution ───────────────────────────────────────────────────────────────


def test_agent_override_wins_over_everything():
    limits = resolve_sandbox_limits(
        "1g",
        1.0,
        settings=SandboxSettings(memory="2g", cpus=2),
        env={ENV_MEMORY: "3g", ENV_CPUS: "3"},
        host=HOST_4C_16G,
    )
    assert (limits.memory, limits.cpus) == ("1g", 1.0)
    assert (limits.memory_source, limits.cpus_source) == ("agent", "agent")


def test_env_beats_config_yaml():
    """Matches config/home.py:_resolve_backend — env over yaml is the repo's
    existing precedence for the same shape of question."""
    limits = resolve_sandbox_limits(
        settings=SandboxSettings(memory="2g", cpus=2),
        env={ENV_MEMORY: "3g", ENV_CPUS: "3"},
        host=HOST_4C_16G,
    )
    assert (limits.memory, limits.cpus) == ("3g", 3.0)
    assert (limits.memory_source, limits.cpus_source) == ("env", "env")


def test_config_yaml_beats_auto():
    limits = resolve_sandbox_limits(
        settings=SandboxSettings(memory="2g", cpus=2), env={}, host=HOST_4C_16G
    )
    assert (limits.memory, limits.cpus) == ("2g", 2.0)
    assert (limits.memory_source, limits.cpus_source) == ("config", "config")


def test_nothing_configured_sizes_from_the_host():
    limits = resolve_sandbox_limits(settings=SandboxSettings(), env={}, host=HOST_4C_16G)
    assert (limits.memory, limits.cpus) == ("4096m", 2.0)
    assert (limits.memory_source, limits.cpus_source) == ("auto", "auto")


def test_undetectable_host_keeps_the_historical_caps():
    limits = resolve_sandbox_limits(settings=SandboxSettings(), env={}, host=NO_HOST)
    assert (limits.memory, limits.cpus) == (FALLBACK_MEMORY, FALLBACK_CPUS)
    assert (limits.memory_source, limits.cpus_source) == ("fallback", "fallback")


def test_each_field_resolves_independently():
    """An agent that pins only memory must still inherit the CPU answer —
    otherwise one override silently drags the other back to a stale default."""
    limits = resolve_sandbox_limits(
        "1g", None, settings=SandboxSettings(), env={ENV_CPUS: "3"}, host=HOST_4C_16G
    )
    assert (limits.memory, limits.memory_source) == ("1g", "agent")
    assert (limits.cpus, limits.cpus_source) == (3.0, "env")


@pytest.mark.parametrize("bad", ["huge", "5m", "", "   "])
def test_an_unusable_memory_env_var_is_ignored_not_fatal(bad, caplog):
    """The variable is deployment-wide: raising would take every agent on the
    box offline over one typo in .env."""
    limits = resolve_sandbox_limits(
        settings=SandboxSettings(), env={ENV_MEMORY: bad}, host=HOST_4C_16G
    )
    assert limits.memory == "4096m"
    assert limits.memory_source == "auto"


@pytest.mark.parametrize("bad", ["many", "0", "999"])
def test_an_unusable_cpu_env_var_is_ignored_not_fatal(bad):
    limits = resolve_sandbox_limits(
        settings=SandboxSettings(), env={ENV_CPUS: bad}, host=HOST_4C_16G
    )
    assert limits.cpus == 2.0
    assert limits.cpus_source == "auto"


def test_resolution_carries_the_host_it_used():
    """The dashboard renders host size next to the limits; they must come from
    one answer, or the card can explain a number it did not derive."""
    limits = resolve_sandbox_limits(settings=SandboxSettings(), env={}, host=HOST_4C_16G)
    assert limits.host is HOST_4C_16G
