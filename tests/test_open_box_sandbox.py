"""An unmanaged box can opt in to per-agent sandbox containers.

The self-host ``setup.sh --sandbox`` writes ``OLANO_SANDBOX_EXEC=1``; with it
an open box takes the managed path's exec rules (no shell in the runtime
container, a sandbox per agent, fail closed when one cannot start) while
staying unmanaged in every other respect.
"""

from __future__ import annotations

import pytest

import olano.core.builder as builder
from olano.config.backend_config import LocalShellBackendConfig
from olano.deployment import enforcement


@pytest.mark.parametrize("name", ["OLANO_SANDBOX_EXEC", "LIONCLAW_SANDBOX_EXEC"])
def test_opt_in_turns_on_sandboxed_exec(monkeypatch, name):
    assert enforcement.require_sandboxed_exec() is False
    monkeypatch.setenv(name, "1")
    assert enforcement.open_box_sandbox_requested() is True
    assert enforcement.require_sandboxed_exec() is True
    # Open posture: the sandbox is always allowed to provide the shell.
    assert enforcement.sandbox_exec_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_falsy_values_leave_the_open_default(monkeypatch, value):
    monkeypatch.setenv("OLANO_SANDBOX_EXEC", value)
    assert enforcement.require_sandboxed_exec() is False


def test_opted_in_local_shell_routes_to_the_docker_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("OLANO_SANDBOX_EXEC", "1")
    sentinel = object()
    monkeypatch.setattr(builder, "_try_build_sandbox_backend", lambda *a, **k: sentinel)
    backend = builder._build_deepagents_backend(
        LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path, agent_name="a"
    )
    assert backend is sentinel


def test_opted_in_box_fails_closed_when_the_sandbox_cannot_start(monkeypatch, tmp_path):
    """The operator asked for isolation: no silent fallback to the runtime container."""
    monkeypatch.setenv("OLANO_SANDBOX_EXEC", "1")
    monkeypatch.setattr(builder, "_try_build_sandbox_backend", lambda *a, **k: None)
    backend = builder._build_deepagents_backend(
        LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path, agent_name="a"
    )
    assert type(backend).__name__ == "FilesystemBackend"


def test_without_the_opt_in_the_open_box_keeps_its_local_shell(tmp_path):
    backend = builder._build_deepagents_backend(
        LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path, agent_name="a"
    )
    assert type(backend).__name__ == "LocalShellBackend"


def test_host_execution_refused_outside_a_sandbox(monkeypatch):
    """The in-process tool seam follows the same rule."""
    from olano.tools import _sandbox_exec

    monkeypatch.setenv("OLANO_SANDBOX_EXEC", "1")
    assert _sandbox_exec._host_execution_forbidden() is True
