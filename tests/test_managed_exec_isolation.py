"""Managed-posture execution isolation (source/secret exfil defense).

The core security guarantee: on a managed deployment an agent configured with a
`local_shell` backend must NOT receive a raw host shell in the runtime
container. It gets either an isolated sandbox (when the operator has enabled it)
or, failing that, a no-shell filesystem backend — never a host shell.
"""

from __future__ import annotations

from pathlib import Path

import olano.core.builder as builder
from olano.config.backend_config import LocalShellBackendConfig


def _backend_type(obj) -> str:
    return type(obj).__name__


def test_open_posture_keeps_local_shell(monkeypatch, tmp_path):
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: False)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)

    b = builder._build_deepagents_backend(
        LocalShellBackendConfig(root_dir=str(tmp_path)), Path(tmp_path)
    )
    assert _backend_type(b) == "LocalShellBackend"


def test_managed_posture_no_sandbox_fails_closed_to_filesystem(monkeypatch, tmp_path):
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: True)  # shell feature on
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)  # managed
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: False)  # sandbox NOT enabled

    b = builder._build_deepagents_backend(
        LocalShellBackendConfig(root_dir=str(tmp_path)), Path(tmp_path)
    )
    # No host shell — a filesystem backend has no `execute` tool at all.
    assert _backend_type(b) == "FilesystemBackend"


def test_managed_shell_feature_off_is_filesystem(monkeypatch, tmp_path):
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: name != "shell")
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)

    b = builder._build_deepagents_backend(
        LocalShellBackendConfig(root_dir=str(tmp_path)), Path(tmp_path)
    )
    assert _backend_type(b) == "FilesystemBackend"


def test_managed_sandbox_enabled_but_docker_unavailable_fails_closed(monkeypatch, tmp_path):
    """Sandbox opted in, but docker can't start → must fail closed, NOT host shell."""
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)
    # Force the sandbox build to fail (as it would with no docker on the box).
    monkeypatch.setattr(builder, "_try_build_sandbox_backend", lambda *a, **k: None)

    b = builder._build_deepagents_backend(
        LocalShellBackendConfig(root_dir=str(tmp_path)), Path(tmp_path)
    )
    assert _backend_type(b) == "FilesystemBackend"  # never LocalShellBackend


def test_shell_routes_to_live_sandbox_not_host(monkeypatch, tmp_path):
    """When a live sandbox backend is present, _run_shell_command must execute in
    it (docker exec) and NEVER fall through to the host run_local."""
    import olano.security.deepagents_middleware as mw
    from olano.backends.lionclaw_docker import OlanoDockerBackend, SandboxRun
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import OlanoDockerBackendConfig

    sandbox = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))
    sandbox._container_name = "olano-test-container"  # liveness needs a container
    calls = {"execute": 0, "host": 0}

    def fake_run(command, **kw):
        calls["execute"] += 1
        return SandboxRun("SANDBOX_OUT", "", 0)

    # `run()` — the split-stream lane the shell summariser uses; deepagents'
    # `execute()` returns a single combined string instead.
    monkeypatch.setattr(sandbox, "run", fake_run)

    def boom_run_local(*a, **k):  # host path must never be hit
        calls["host"] += 1
        raise AssertionError("host run_local must not be called for a sandbox agent")

    monkeypatch.setattr("olano.security.shell_guard.run_local", boom_run_local)

    cfg = AgentConfig(name="t", system_prompt="x")
    msg = mw._run_shell_command(
        tool_name="execute",
        args={"command": "echo hi", "timeout": 10},
        config=cfg,
        workspace_root=Path(tmp_path),
        mounts=[],
        tool_call_id="c1",
        backend=sandbox,
    )
    assert calls["execute"] == 1 and calls["host"] == 0
    assert "SANDBOX_OUT" in (msg.content or "")


def test_managed_shell_tool_in_tools_list_is_blocked_not_host(monkeypatch, tmp_path):
    """Bypass guard: even if an agent lists a shell tool (has_shell_access via the
    tools list) with a coerced filesystem backend, managed posture must refuse —
    never fall through to run_local on the host."""
    import olano.deployment.enforcement as enf
    import olano.security.deepagents_middleware as mw
    from olano.config.agent_config import AgentConfig

    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)

    def boom_run_local(*a, **k):
        raise AssertionError("host run_local must never run under managed posture")

    monkeypatch.setattr("olano.security.shell_guard.run_local", boom_run_local)

    cfg = AgentConfig(name="t", system_prompt="x")
    msg = mw._run_shell_command(
        tool_name="safe_shell",
        args={"command": "cat /app/olano/core/builder.py", "timeout": 10},
        config=cfg,
        workspace_root=Path(tmp_path),
        mounts=[],
        tool_call_id="c1",
        backend=None,  # no live sandbox (coerced filesystem backend)
    )
    assert msg.status == "error"
    assert "disabled on this managed deployment" in (msg.content or "")


# ── Terminal → sandbox (dashboard PTY on managed boxes) ───────────────────────


def test_terminal_enabled_open_posture_is_true(monkeypatch):
    """Open/unmanaged: the terminal is a host shell, enabled by default."""
    import olano.deployment.enforcement as enf
    from olano.runtime import terminal as term

    monkeypatch.setattr(enf, "feature_enabled", lambda name: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: False)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: False)
    assert term.terminal_enabled() is True


def test_terminal_enabled_managed_follows_sandbox_exec(monkeypatch):
    """Managed: the terminal is available iff sandbox exec is enabled (it opens
    INTO the sandbox, never a host shell in the runtime container)."""
    import olano.deployment.enforcement as enf
    from olano.runtime import terminal as term

    monkeypatch.setattr(enf, "feature_enabled", lambda name: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)

    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)
    assert term.terminal_enabled() is True  # opens into the sandbox

    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: False)
    assert term.terminal_enabled() is False  # no sandbox ⇒ no safe shell


def test_terminal_enabled_managed_requires_shell_feature(monkeypatch):
    """No shell entitlement ⇒ no PTY, even with sandbox exec on."""
    import olano.deployment.enforcement as enf
    from olano.runtime import terminal as term

    monkeypatch.setattr(enf, "feature_enabled", lambda name: name != "shell")
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)
    assert term.terminal_enabled() is False


def test_terminal_uses_sandbox_tracks_posture(monkeypatch):
    import olano.deployment.enforcement as enf
    from olano.runtime import terminal as term

    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    assert term.terminal_uses_sandbox() is True
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: False)
    assert term.terminal_uses_sandbox() is False


def test_sandbox_container_ref_resolves_live_backend(monkeypatch):
    """The terminal resolves the live sandbox container from the build registry."""
    import olano.core.builder as builder
    from olano.runtime import terminal as term

    class _FakeBackend:
        def container_ref(self):
            return "olano-merli-abc123"

    monkeypatch.setattr(builder, "_SANDBOX_BACKENDS", {"merli": _FakeBackend()})

    assert term.sandbox_container_ref("merli") == "olano-merli-abc123"
    # No agent / unknown agent / no sandbox ⇒ None (caller must refuse, never
    # fall back to a host shell).
    assert term.sandbox_container_ref(None) is None
    assert term.sandbox_container_ref("ghost") is None


class _StopBridge(Exception):
    """Sentinel to break run_pty_bridge right after the spawn selection."""


async def test_sandbox_spawn_selected_when_container_present(monkeypatch):
    """run_pty_bridge forks the docker-exec child (not a host shell) when given a
    container; the host `_spawn_shell` must not run."""
    from olano.runtime import terminal as term

    picked = {"sandbox": None}

    def fake_sandbox_spawn(container):
        picked["sandbox"] = container
        raise _StopBridge

    def boom_host(cwd):
        raise AssertionError("host shell spawned despite a sandbox container")

    monkeypatch.setattr(term, "_spawn_sandbox_shell", fake_sandbox_spawn)
    monkeypatch.setattr(term, "_spawn_shell", boom_host)

    class _WS:
        headers: dict = {}

    try:
        await term.run_pty_bridge(_WS(), cwd=Path("/tmp"), container="olano-x")
    except _StopBridge:
        pass
    assert picked["sandbox"] == "olano-x"


class _Execed(Exception):
    """Sentinel to break out of the forked-child branch during the exec."""


def test_sandbox_pty_execs_interactive_bash_with_sh_fallback(monkeypatch):
    """The sandbox PTY must not land the user in dash.

    Debian's /bin/sh has no readline, so a bare `sh -l` gave a terminal where Tab
    completed nothing and ↑ printed `^[[A`. The child now execs bash when the
    container has one — and must still fall back to `sh -l` when it doesn't,
    since the shell is chosen inside a container this code cannot inspect.
    """
    import os

    from olano.runtime import terminal as term

    captured: dict = {}

    def fake_execvpe(file, argv, env):
        captured["file"] = file
        captured["argv"] = argv
        raise _Execed

    def fake_exit(code):  # the child's last resort — never kill the test process
        raise _Execed

    monkeypatch.setattr(term.pty, "fork", lambda: (0, 7))  # 0 ⇒ run the child branch
    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    monkeypatch.setattr(os, "_exit", fake_exit)

    try:
        term._spawn_sandbox_shell("olano-x")
    except _Execed:
        pass

    assert captured["file"] == "docker"
    assert captured["argv"][:6] == ["docker", "exec", "-it", "olano-x", "sh", "-c"]
    bootstrap = captured["argv"][6]
    assert "exec bash -l" in bootstrap
    assert bootstrap.rstrip().endswith("exec sh -l")  # fallback stays last
    assert "command -v bash" in bootstrap  # POSIX probe: runs under dash itself


# ── Explicit `backend: {type: "lionclaw_docker"}` ─────────────────────────────
#
# This branch used to return an UNSTARTED OlanoDockerBackend: no container, no
# chown, no registry entry. The agent booted clean and reported healthy while
# every file and shell tool answered exit 127 ("start() has not been called"),
# and `olano_run_agent_command` refused with "has none live right now" because
# nothing was ever written to _SANDBOX_BACKENDS.


def _docker_cfg(**kw):
    from olano.config.backend_config import OlanoDockerBackendConfig

    return OlanoDockerBackendConfig(**kw)


def _posture(monkeypatch, *, shell=True, managed=True, sandbox=True):
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: shell)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: managed)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: sandbox)


def _capture_start(monkeypatch):
    """Record start() kwargs and bind a container, without touching Docker."""
    import olano.backends.lionclaw_docker as ld

    calls: list[dict] = []

    def fake_start(self, *, agent_name, workspace_root, shared_mounts=None):
        calls.append(
            {
                "backend": self,
                "agent_name": agent_name,
                "workspace_root": workspace_root,
                "shared_mounts": shared_mounts,
            }
        )
        self._container_name = f"lionclaw-{agent_name}-test"

    monkeypatch.setattr(ld.OlanoDockerBackend, "start", fake_start)
    monkeypatch.setattr(builder, "_seed_sandbox_tool_sets", lambda b: None)
    return calls


def test_explicit_docker_backend_is_started_and_registered(monkeypatch, tmp_path):
    """The regression test: an explicit docker backend must actually start."""
    _posture(monkeypatch)
    calls = _capture_start(monkeypatch)
    chowns: list[tuple] = []
    monkeypatch.setattr(
        builder,
        "_ensure_sandbox_shared_dir",
        lambda p, recursive=False: chowns.append((p, recursive)),
    )

    b = builder._build_deepagents_backend(
        _docker_cfg(persistent=True),
        Path(tmp_path),
        agent_name="alpha",
        shared_mounts=[("/host", "/container", "rw")],
    )

    assert _backend_type(b) == "OlanoDockerBackend"
    assert len(calls) == 1
    assert calls[0]["agent_name"] == "alpha"
    assert calls[0]["workspace_root"] == Path(tmp_path)
    assert calls[0]["shared_mounts"] == [("/host", "/container", "rw")]
    # Registered, or the terminal / chat `!` / sandboxed_run() cannot reach it.
    assert builder.get_sandbox_backend("alpha") is b
    assert b.container_ref() == "lionclaw-alpha-test"
    # Recursive, or the uid-1000 agent cannot edit anything the root runtime seeded.
    assert chowns == [(Path(tmp_path), True)]
    assert builder.sandbox_status("alpha")["state"] == builder.SANDBOX_STARTED


def test_explicit_docker_backend_fails_closed_when_sandbox_exec_off(monkeypatch, tmp_path):
    """Naming the backend explicitly is not a way around the operator's opt-in."""
    _posture(monkeypatch, sandbox=False)
    _capture_start(monkeypatch)

    b = builder._build_deepagents_backend(
        _docker_cfg(persistent=True), Path(tmp_path), agent_name="alpha"
    )

    assert _backend_type(b) == "FilesystemBackend"
    assert builder.get_sandbox_backend("alpha") is None
    assert builder.sandbox_status("alpha")["state"] == builder.SANDBOX_NOT_ENTITLED


def test_explicit_docker_backend_fails_closed_when_shell_feature_off(monkeypatch, tmp_path):
    _posture(monkeypatch, shell=False)
    _capture_start(monkeypatch)

    b = builder._build_deepagents_backend(
        _docker_cfg(persistent=True), Path(tmp_path), agent_name="alpha"
    )

    assert _backend_type(b) == "FilesystemBackend"
    assert builder.get_sandbox_backend("alpha") is None
    assert builder.sandbox_status("alpha")["state"] == builder.SANDBOX_SHELL_DISABLED


def test_explicit_docker_start_failure_fails_closed_not_raises(monkeypatch, tmp_path):
    """A dead docker daemon must not kill the boot, and must not half-register."""
    import olano.backends.lionclaw_docker as ld

    _posture(monkeypatch)
    monkeypatch.setattr(builder, "_seed_sandbox_tool_sets", lambda b: None)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda p, recursive=False: None)

    def boom(self, **kw):
        raise RuntimeError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(ld.OlanoDockerBackend, "start", boom)

    b = builder._build_deepagents_backend(
        _docker_cfg(persistent=True), Path(tmp_path), agent_name="alpha"
    )

    assert _backend_type(b) == "FilesystemBackend"
    assert builder.get_sandbox_backend("alpha") is None  # never half-registered
    assert builder.sandbox_status("alpha")["state"] == builder.SANDBOX_START_FAILED


def test_explicit_docker_host_network_is_coerced_on_a_managed_box(monkeypatch, tmp_path):
    """network=host would reach every 127.0.0.1 service on the host."""
    _posture(monkeypatch)
    calls = _capture_start(monkeypatch)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda p, recursive=False: None)

    cfg = _docker_cfg(persistent=True, network="host")
    b = builder._build_deepagents_backend(cfg, Path(tmp_path), agent_name="alpha")

    assert b._config.network == "bridge"
    assert cfg.network == "host"  # the caller's config object is never mutated
    assert len(calls) == 1


def test_explicit_docker_host_network_is_kept_on_an_open_box(monkeypatch, tmp_path):
    """The operator's own machine: their container, their rules."""
    _posture(monkeypatch, managed=False)
    _capture_start(monkeypatch)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda p, recursive=False: None)

    b = builder._build_deepagents_backend(
        _docker_cfg(persistent=True, network="host"), Path(tmp_path), agent_name="alpha"
    )

    assert b._config.network == "host"


def test_explicit_docker_custom_image_is_refused_on_a_managed_box(monkeypatch, tmp_path):
    """The run flags set no --user, so the image decides whether the agent is root."""
    monkeypatch.delenv("LIONCLAW_SANDBOX_IMAGE", raising=False)
    _posture(monkeypatch)
    _capture_start(monkeypatch)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda p, recursive=False: None)

    b = builder._build_deepagents_backend(
        _docker_cfg(persistent=True, image="ubuntu:latest"), Path(tmp_path), agent_name="alpha"
    )

    assert b._config.image == "olano-sandbox:latest"


def test_explicit_docker_ephemeral_is_forced_persistent_on_a_managed_box(monkeypatch, tmp_path):
    """Nothing calls stop(), so `--rm` never fires and each build would leak one."""
    _posture(monkeypatch)
    _capture_start(monkeypatch)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda p, recursive=False: None)

    b = builder._build_deepagents_backend(
        _docker_cfg(persistent=False), Path(tmp_path), agent_name="alpha"
    )

    assert b._config.persistent is True


def test_explicit_docker_keeps_its_own_config_on_an_open_box(monkeypatch, tmp_path):
    """Pins that the explicit branch is not just an alias for the hardened synthesis."""
    _posture(monkeypatch, managed=False)
    _capture_start(monkeypatch)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda p, recursive=False: None)

    cfg = _docker_cfg(persistent=False, image="my-sandbox:dev", snapshot_on_stop=False)
    b = builder._build_deepagents_backend(cfg, Path(tmp_path), agent_name="alpha")

    assert b._config.persistent is False
    assert b._config.image == "my-sandbox:dev"
    assert b._config.snapshot_on_stop is False


def test_managed_shell_refuses_when_the_sandbox_never_started(monkeypatch, tmp_path):
    """A backend with no container must refuse, not `docker exec` into nothing.

    `_is_live_sandbox_backend` was an isinstance check, so an unstarted backend
    sailed through the managed chokepoint and the command was routed into a
    container that had never been created — surfacing as exit 127 rather than
    an actionable refusal.
    """
    import olano.deployment.enforcement as enf
    import olano.security.deepagents_middleware as mw
    from olano.backends.lionclaw_docker import OlanoDockerBackend
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import OlanoDockerBackendConfig

    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    sandbox = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))  # never started

    def boom_run(*a, **k):
        raise AssertionError("must not exec into a container that does not exist")

    monkeypatch.setattr(sandbox, "run", boom_run)
    monkeypatch.setattr(
        "olano.security.shell_guard.run_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run on the host")),
    )

    msg = mw._run_shell_command(
        tool_name="execute",
        args={"command": "echo hi", "timeout": 10},
        config=AgentConfig(name="t", system_prompt="x"),
        workspace_root=Path(tmp_path),
        mounts=[],
        tool_call_id="c1",
        backend=sandbox,
    )

    assert "sandbox container is not running" in (msg.content or "")


def test_open_posture_shell_refuses_a_dead_sandbox_instead_of_the_host(monkeypatch, tmp_path):
    """An agent that asked for a container must not silently get the host."""
    import olano.deployment.enforcement as enf
    import olano.security.deepagents_middleware as mw
    from olano.backends.lionclaw_docker import OlanoDockerBackend
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import OlanoDockerBackendConfig

    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: False)  # open box
    monkeypatch.setattr(
        "olano.security.shell_guard.run_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run on the host")),
    )

    msg = mw._run_shell_command(
        tool_name="execute",
        args={"command": "echo hi", "timeout": 10},
        config=AgentConfig(name="t", system_prompt="x"),
        workspace_root=Path(tmp_path),
        mounts=[],
        tool_call_id="c1",
        backend=OlanoDockerBackend(OlanoDockerBackendConfig()),  # never started
    )

    assert "sandbox container is not running" in (msg.content or "")


def test_sandbox_pty_prefers_the_runtime_rc_file_so_the_tool_layer_is_on_path(monkeypatch):
    """`bash -l` sources /etc/profile, which resets PATH — so nothing under
    /opt/agent-tools (pip --user scripts, npm globals, the `python` shim) was
    reachable from the Terminal. When the runtime has written
    /opt/agent-tools/shims/terminal.bashrc the child execs bash with it as the
    rc file; when it hasn't, the plain login shell (and the sh fallback) stay."""
    import os

    from olano.runtime import terminal as term

    captured: dict = {}

    def fake_execvpe(file, argv, env):
        captured["argv"] = argv
        raise _Execed

    monkeypatch.setattr(term.pty, "fork", lambda: (0, 7))
    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    monkeypatch.setattr(os, "_exit", lambda code: (_ for _ in ()).throw(_Execed()))

    try:
        term._spawn_sandbox_shell("olano-x")
    except _Execed:
        pass

    bootstrap = captured["argv"][6]
    rc = "/opt/agent-tools/shims/terminal.bashrc"
    assert bootstrap.startswith(f"f={rc}; ")
    assert 'if [ -r "$f" ]; then exec bash --rcfile "$f" -i; fi' in bootstrap
    assert bootstrap.index("--rcfile") < bootstrap.index("exec bash -l")
    assert bootstrap.rstrip().endswith("exec sh -l")
