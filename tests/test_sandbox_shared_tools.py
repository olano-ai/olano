"""Shared tool layer for the managed exec sandbox.

Every agent's sandbox bind-mounts one home-scoped host dir at /opt/agent-tools
with pip/npm pointed at it, so a package installed by one agent is visible to
all — install once, shared across agents, survives restarts and image rebuilds.
The home ``shared/`` dir is surfaced under /shared. These tests pin the wiring
(mounts + env) without needing a live Docker daemon.
"""

from __future__ import annotations

import olano.backends.interpreter_shims as interpreter_shims
import olano.core.builder as builder
from olano.backends.lionclaw_docker import (
    _TOOL_LAYER_DIR,
    _TOOL_LAYER_ENV,
    OlanoDockerBackend,
)
from olano.config.agent_config import AgentConfig
from olano.config.backend_config import OlanoDockerBackendConfig


def test_resolve_shared_mounts_includes_tool_layer_and_shared(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    cfg = AgentConfig(name="alpha", system_prompt="x")

    mounts = builder._resolve_sandbox_shared_mounts(cfg, tmp_path / "ws")

    targets = {container for _host, container, _mode in mounts}
    # The tool layer is always present and points at the home-scoped host dir.
    assert _TOOL_LAYER_DIR in targets
    tool_host = next(h for h, c, _m in mounts if c == _TOOL_LAYER_DIR)
    assert tool_host == str(tmp_path / "sandbox" / "tools")
    assert (tmp_path / "sandbox" / "tools").is_dir()  # created, writable
    # The home shared/ dir is surfaced under /shared/* (default grant).
    assert any(c.startswith("/shared") for _h, c, _m in mounts)


def test_tool_layer_host_dir_is_the_same_for_every_agent(monkeypatch, tmp_path):
    """Two different agents resolve to the SAME tool-layer host dir → shared."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    a = builder._resolve_sandbox_shared_mounts(
        AgentConfig(name="alpha", system_prompt="x"), tmp_path / "a"
    )
    b = builder._resolve_sandbox_shared_mounts(
        AgentConfig(name="beta", system_prompt="x"), tmp_path / "b"
    )
    tool_a = next(h for h, c, _m in a if c == _TOOL_LAYER_DIR)
    tool_b = next(h for h, c, _m in b if c == _TOOL_LAYER_DIR)
    assert tool_a == tool_b  # install once, all agents see it


def test_run_args_carry_tool_layer_env_and_mounts(tmp_path):
    b = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True, network="bridge"))
    b._workspace_root = tmp_path
    b._extra_mounts = [(str(tmp_path / "tools"), _TOOL_LAYER_DIR, "rw")]

    args = b._base_run_args()
    joined = " ".join(args)
    # Installers are pointed at the shared layer.
    for key, val in _TOOL_LAYER_ENV.items():
        assert f"{key}={val}" in args
    # Workspace + the shared tool mount are both bind-mounted.
    assert f"{tmp_path.resolve()}:/workspace:rw" in args
    assert f"{tmp_path / 'tools'}:{_TOOL_LAYER_DIR}:rw" in joined


def test_run_args_carry_the_configured_resource_limits(tmp_path):
    """The caps were hardcoded at 512m/0.5, so a 16 GB box gave its agents the
    same room as a 1 GB one. Whatever the builder resolves must reach docker."""
    b = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="bridge", memory="4g", cpus=2.0)
    )
    b._workspace_root = tmp_path
    b._extra_mounts = []

    args = b._base_run_args()
    assert "--memory=4g" in args
    assert "--cpus=2.0" in args


def test_run_args_fall_back_to_the_historical_caps(tmp_path):
    """A backend nobody resolved limits for behaves exactly as it always did."""
    b = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True, network="bridge"))
    b._workspace_root = tmp_path
    b._extra_mounts = []

    args = b._base_run_args()
    assert "--memory=512m" in args
    assert "--cpus=0.5" in args


def test_exec_prefixes_tool_layer_env_and_path(monkeypatch, tmp_path):
    """Every command gets the tool-layer env + PATH prepend so shared console
    scripts and PYTHONUSERBASE take effect inside the shell."""
    b = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))
    b._container_name = "olano-test"

    captured = {}

    class _Res:
        stdout, stderr, returncode = "", "", 0

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _Res()

    monkeypatch.setattr("olano.backends.lionclaw_docker.subprocess.run", fake_run)
    b.execute("python3 -m pip install requests")

    shell_cmd = captured["cmd"][-1]  # the `sh -c <this>` payload
    assert "export PYTHONUSERBASE=/opt/agent-tools/python" in shell_cmd
    assert (
        "export PATH=/opt/agent-tools/python/bin:/opt/agent-tools/npm/bin:/opt/agent-tools/bin:$PATH"
        in shell_cmd
    )
    assert shell_cmd.rstrip().endswith("python3 -m pip install requests")


def test_exec_appends_the_interpreter_shims_after_the_image_path(monkeypatch):
    """`python` must resolve on a stale image with no `python-is-python3`, yet a
    real `python` (image alias, venv) must still win — so the shim dir goes
    AFTER $PATH, never before it. Both the exec prefix and the stdio-MCP launch
    line take it from the one `shell_path_export()`."""
    from olano.backends.interpreter_shims import CONTAINER_SHIM_PATH, shell_path_export
    from olano.backends.lionclaw_docker import _TOOL_LAYER_SHIM_PATH, mcp_stdio_shell_script

    b = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))
    b._container_name = "olano-test"
    captured = {}

    class _Res:
        stdout, stderr, returncode = "", "", 0

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _Res()

    monkeypatch.setattr("olano.backends.lionclaw_docker.subprocess.run", fake_run)
    b.execute("python -V")

    shell_cmd = captured["cmd"][-1]
    assert _TOOL_LAYER_SHIM_PATH == CONTAINER_SHIM_PATH == "/opt/agent-tools/shims"
    assert shell_path_export() in shell_cmd
    assert ":$PATH:/opt/agent-tools/shims" in shell_cmd
    assert shell_cmd.index("/opt/agent-tools/bin:$PATH") < shell_cmd.index(
        ":/opt/agent-tools/shims"
    )
    assert mcp_stdio_shell_script("npx", ["-y", "x"]).startswith(shell_path_export() + "; exec ")


def test_resolve_shared_mounts_writes_the_interpreter_shims_into_the_tool_layer(
    monkeypatch, tmp_path
):
    """The shims live on the HOST side of the tool-layer mount, so they reach
    containers that already exist and images that lack the alias."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    cfg = AgentConfig(name="alpha", system_prompt="x")

    mounts = builder._resolve_sandbox_shared_mounts(cfg, tmp_path / "ws")

    host = next(h for h, c, _m in mounts if c == _TOOL_LAYER_DIR)
    shim = tmp_path / "sandbox" / "tools" / "shims"
    assert host == str(shim.parent)
    assert (shim / "python").read_text().startswith("#!/bin/sh")
    assert (shim / "pip").exists()
    assert (shim / "terminal.bashrc").exists()


def test_tool_layer_install_targets_exist_on_the_host_side(monkeypatch, tmp_path):
    """npm's prefix (and pip's user base) must exist BEFORE anything installs.

    The bind mount shadows the image's own ``/opt/agent-tools/{python,npm,…}``,
    so only what the host side creates is visible in the container. npm never
    creates ``npm_config_prefix`` and npm 9 (the image's) lstats it on every
    ``npx``, so a missing ``npm/`` here is every ``npx`` — and every
    npx-launched stdio MCP server — dying ENOENT inside the sandbox.
    """
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    cfg = AgentConfig(name="alpha", system_prompt="x")

    builder._resolve_sandbox_shared_mounts(cfg, tmp_path / "ws")

    tool_dir = tmp_path / "sandbox" / "tools"
    for sub in interpreter_shims.TOOL_LAYER_INSTALL_SUBDIRS:
        assert (tool_dir / sub).is_dir(), f"tool layer is missing {sub}/"
    # Measured on npm 9.2.0 (the image's): `npm/` alone moves the ENOENT to
    # `npm/lib`; `npm/lib` is what npx lstats. Pin the skeleton, not just the
    # prefix, so a "simplification" back to the top-level dir fails here.
    assert (tool_dir / "npm" / "lib" / "node_modules").is_dir()
    assert (tool_dir / "npm" / "bin").is_dir()


def test_stdio_mcp_preparer_creates_the_same_layout(monkeypatch, tmp_path):
    """The ephemeral stdio-MCP / job containers mount the layer WITHOUT going
    through the agent-sandbox resolver — on a box whose agents run the plain
    filesystem posture, this is the only preparer that ever runs, and an
    `npx`-launched MCP server needs the npm skeleton exactly as much."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    from olano.core import mcp_setup

    mcp_setup._STDIO_SANDBOX_PREPARED.clear()
    mcp_setup._prepare_stdio_sandbox_paths("alpha", tmp_path / "ws")

    tool_dir = tmp_path / "sandbox" / "tools"
    for sub in interpreter_shims.TOOL_LAYER_INSTALL_SUBDIRS:
        assert (tool_dir / sub).is_dir(), f"stdio preparer left out {sub}/"


def test_layout_is_derived_from_the_env_and_path_that_name_it(monkeypatch):
    """A new install target added to the env gets its directory by construction."""
    monkeypatch.setitem(
        interpreter_shims.CONTAINER_TOOL_LAYER_ENV,
        "CARGO_HOME",
        f"{interpreter_shims.CONTAINER_TOOL_LAYER_DIR}/cargo",
    )
    layout = interpreter_shims.tool_layer_layout()
    assert "cargo" in layout
    # Ancestors precede children so every level can be handed to the sandbox uid.
    assert layout.index("npm") < layout.index("npm/lib") < layout.index("npm/lib/node_modules")
    assert layout.index("cache") < layout.index("cache/npm")


def test_every_tool_layer_env_path_is_created_or_self_creating(monkeypatch, tmp_path):
    """Each ``CONTAINER_TOOL_LAYER_ENV`` path resolves under a created subdir.

    Pins the two lists together: an install target added to the env without a
    matching entry in ``TOOL_LAYER_INSTALL_SUBDIRS`` reproduces the npm bug.
    """
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    builder._resolve_sandbox_shared_mounts(
        AgentConfig(name="alpha", system_prompt="x"), tmp_path / "ws"
    )

    created = set(interpreter_shims.TOOL_LAYER_INSTALL_SUBDIRS)
    for key, value in interpreter_shims.CONTAINER_TOOL_LAYER_ENV.items():
        if not value.startswith(f"{interpreter_shims.CONTAINER_TOOL_LAYER_DIR}/"):
            continue  # a flag like PIP_USER=1, not a path
        top = value[len(interpreter_shims.CONTAINER_TOOL_LAYER_DIR) + 1 :].split("/")[0]
        assert top in created, f"{key}={value} points outside the created subdirs"
