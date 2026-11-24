"""A non-execute agent still gets its sandbox container on a managed box.

The container used to exist only for agents whose deepagents backend is
shell-capable (`local_shell` coerced under managed posture, or an explicit
`lionclaw_docker`). A "Read & Write" agent got no container at all — so the
dashboard Terminal closed 4503 ("sandbox isn't running — start the agent, then
reconnect", which starting the agent could never fix), the chat `!` lane
refused, and in-process tools that shell out (`sandboxed_run`) fell back to
the RUNTIME HOST, the very thing the managed posture exists to prevent.

`builder._start_sidecar_sandbox` now starts the same persistent container
beside a plain filesystem/virtual backend and records it with `role: sidecar`.
These tests pin the boundary that makes that safe: the agent's own backend is
untouched (no `execute`, permission rules intact), the security middleware
still refuses shell tools for it, the prompt does not claim its tools run in a
container, and every surface that reads the registry — Terminal, chat `!`,
`sandboxed_run`, diagnostics — can reach the sidecar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import olano.core.builder as builder
from olano.backends.lionclaw_docker import OlanoDockerBackend, SandboxRun
from olano.config.agent_config import AgentConfig
from olano.config.backend_config import (
    CompositeBackendConfig,
    DaytonaBackendConfig,
    FilesystemBackendConfig,
    LocalShellBackendConfig,
    StateBackendConfig,
    StoreBackendConfig,
)

FAKE_CONTAINER = "lionclaw-sidecar-test"


def _posture(monkeypatch, *, shell=True, managed=True, sandbox=True):
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: shell)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: managed)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: sandbox)


def _startable(monkeypatch):
    """Let the real start path run without docker: start() binds a container name."""

    def fake_start(self, *, agent_name, workspace_root, shared_mounts=None):
        self._agent_name = agent_name
        self._workspace_root = workspace_root
        self._container_name = FAKE_CONTAINER

    monkeypatch.setattr(OlanoDockerBackend, "start", fake_start)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda *a, **k: None)


def _cfg(backend, name="rw"):
    return AgentConfig(name=name, system_prompt="x", backend=backend)


# ── who gets a sidecar ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "backend",
    [
        FilesystemBackendConfig(root_dir="workspace", access_mode="read_write"),
        FilesystemBackendConfig(root_dir="workspace", access_mode="read"),
        StateBackendConfig(),
        StoreBackendConfig(),
        CompositeBackendConfig(default=FilesystemBackendConfig(root_dir="workspace")),
    ],
    ids=["read_write", "read", "state", "store", "composite"],
)
def test_managed_entitled_box_starts_a_sidecar_for_every_local_backend(
    monkeypatch, tmp_path, backend
):
    _posture(monkeypatch)
    _startable(monkeypatch)
    cfg = _cfg(backend)

    sb = builder._start_sidecar_sandbox(cfg, tmp_path)

    assert sb is not None and builder.get_sandbox_backend("rw") is sb
    assert sb.container_ref() == FAKE_CONTAINER
    status = builder.sandbox_status("rw")
    assert status["state"] == builder.SANDBOX_STARTED
    assert status["role"] == builder.SANDBOX_ROLE_SIDECAR
    assert builder.sandbox_role("rw") == "sidecar"


def test_sidecar_uses_the_execute_paths_container_identity(monkeypatch, tmp_path):
    """Flipping Access to Execute later must reattach THIS container, so the
    sidecar is built from the same synthesis the local_shell coercion uses."""
    _posture(monkeypatch)
    seen: dict = {}

    def fake_try(local_cfg, *, workspace_root, agent_name, shared_mounts, sandbox_overrides, role):
        seen.update(
            root=getattr(local_cfg, "root_dir", None),
            ws=workspace_root,
            agent=agent_name,
            role=role,
        )
        return object()

    monkeypatch.setattr(builder, "_try_build_sandbox_backend", fake_try)
    cfg = _cfg(FilesystemBackendConfig(root_dir="workspace"))
    assert builder._start_sidecar_sandbox(cfg, tmp_path) is not None
    assert seen == {
        "root": "workspace",
        "ws": tmp_path,
        "agent": "rw",
        "role": builder.SANDBOX_ROLE_SIDECAR,
    }


def test_open_posture_never_starts_a_sidecar(monkeypatch, tmp_path):
    """The Terminal is a host shell on the operator's own box — no container."""
    _posture(monkeypatch, managed=False)
    _startable(monkeypatch)
    assert builder._start_sidecar_sandbox(_cfg(FilesystemBackendConfig()), tmp_path) is None
    assert builder.get_sandbox_backend("rw") is None
    assert builder.sandbox_status("rw") is None


@pytest.mark.parametrize(
    "kw", [{"sandbox": False}, {"shell": False}], ids=["not_entitled", "no_shell"]
)
def test_managed_box_without_the_entitlement_starts_nothing(monkeypatch, tmp_path, kw):
    _posture(monkeypatch, **kw)
    _startable(monkeypatch)
    assert builder._start_sidecar_sandbox(_cfg(FilesystemBackendConfig()), tmp_path) is None
    assert builder.get_sandbox_backend("rw") is None
    assert builder.sandbox_status("rw") is None  # unchanged: never wanted one


def test_remote_sandbox_backend_gets_no_sidecar(monkeypatch, tmp_path):
    _posture(monkeypatch)
    _startable(monkeypatch)
    assert builder._start_sidecar_sandbox(_cfg(DaytonaBackendConfig()), tmp_path) is None
    assert builder.get_sandbox_backend("rw") is None


def test_sidecar_defers_to_a_backend_decision_already_recorded(monkeypatch, tmp_path):
    """A local_shell agent whose sandbox FAILED must not get a second attempt
    relabelled as a sidecar; the recorded failure is the diagnosis."""
    _posture(monkeypatch)
    _startable(monkeypatch)
    builder._record_sandbox_status("rw", builder.SANDBOX_START_FAILED, "docker exploded")
    assert builder._start_sidecar_sandbox(_cfg(LocalShellBackendConfig()), tmp_path) is None
    assert builder.sandbox_status("rw")["state"] == builder.SANDBOX_START_FAILED


def test_sidecar_is_idempotent_when_a_backend_sandbox_is_live(monkeypatch, tmp_path):
    _posture(monkeypatch)
    calls: list = []
    monkeypatch.setattr(builder, "_try_build_sandbox_backend", lambda *a, **k: calls.append(1))
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, "rw", object())
    assert builder._start_sidecar_sandbox(_cfg(FilesystemBackendConfig()), tmp_path) is None
    assert calls == []


def test_start_failure_is_recorded_as_a_sidecar_and_never_raises(monkeypatch, tmp_path):
    _posture(monkeypatch)  # conftest patches start() to raise
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda *a, **k: None)
    assert builder._start_sidecar_sandbox(_cfg(FilesystemBackendConfig()), tmp_path) is None
    status = builder.sandbox_status("rw")
    assert status["state"] == builder.SANDBOX_START_FAILED
    assert status["role"] == builder.SANDBOX_ROLE_SIDECAR
    assert builder.get_sandbox_backend("rw") is None


# ── the boundary: a sidecar never hands the MODEL a shell ─────────────────────


def test_sidecar_leaves_the_agents_own_backend_alone(monkeypatch, tmp_path):
    """The deepagents backend a Read & Write agent gets is exactly what it got
    before — a FilesystemBackend, which is what keeps `execute` ungranted and
    the read-only / multi-user permission rules enforceable."""
    _posture(monkeypatch)
    _startable(monkeypatch)
    cfg = _cfg(FilesystemBackendConfig(root_dir=str(tmp_path), access_mode="read"))
    backend = builder._build_deepagents_backend(cfg.backend, tmp_path, agent_name="rw")
    builder._start_sidecar_sandbox(cfg, tmp_path)
    assert type(backend).__name__ == "FilesystemBackend"
    assert builder.get_sandbox_backend("rw") is not None  # sidecar live beside it
    perms = builder._build_permissions(cfg, tmp_path)
    assert any(getattr(p, "mode", None) == "deny" for p in perms)


def test_shell_tools_stay_refused_while_a_sidecar_is_live(monkeypatch, tmp_path):
    """The middleware routes shell from the backend the builder passed it, never
    from the registry — so a sidecar cannot become an `execute` lane for a
    Read & Write agent that lists `safe_shell` in `tools:`."""
    import olano.security.deepagents_middleware as mw

    _posture(monkeypatch)
    _startable(monkeypatch)
    cfg = _cfg(FilesystemBackendConfig())
    sb = builder._start_sidecar_sandbox(cfg, tmp_path)
    assert sb is not None

    def boom(*a, **k):
        raise AssertionError("sidecar must not run the model's shell command")

    monkeypatch.setattr(sb, "run", boom)
    monkeypatch.setattr(
        "olano.security.shell_guard.run_local",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("host shell")),
    )
    msg = mw._run_shell_command(
        tool_name="safe_shell",
        args={"command": "id", "timeout": 5},
        config=cfg,
        workspace_root=Path(tmp_path),
        mounts=[],
        tool_call_id="c1",
        backend=None,  # the agent's real backend is a FilesystemBackend
    )
    assert msg.status == "error"
    assert "disabled on this managed deployment" in (msg.content or "")


def test_prompt_does_not_claim_sandboxed_tools_for_a_sidecar(monkeypatch, tmp_path):
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    _posture(monkeypatch)
    _startable(monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _cfg(FilesystemBackendConfig())
    builder._start_sidecar_sandbox(cfg, ws)
    assert builder.sandbox_is_backend("rw") is False

    block = PromptAssembler(cfg, Workspace(root=ws))._build_workspace_environment()
    assert "isolated sandbox container" not in block


def test_prompt_still_claims_sandboxed_tools_when_the_sandbox_is_the_backend(monkeypatch, tmp_path):
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _cfg(LocalShellBackendConfig())
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, "rw", object())
    builder._record_sandbox_status("rw", builder.SANDBOX_STARTED, "live", container="c")
    assert builder.sandbox_is_backend("rw") is True

    block = PromptAssembler(cfg, Workspace(root=ws))._build_workspace_environment()
    assert "isolated sandbox container" in block


# ── the surfaces that read the registry reach the sidecar ─────────────────────


def _live_sidecar(monkeypatch, tmp_path, recorder=None):
    _posture(monkeypatch)
    _startable(monkeypatch)
    sb = builder._start_sidecar_sandbox(_cfg(FilesystemBackendConfig()), tmp_path)
    assert sb is not None
    if recorder is not None:

        def fake_run(command, **kw):
            recorder["cmd"] = command
            return SandboxRun("SIDECAR_OUT", "", 0)

        monkeypatch.setattr(sb, "run", fake_run)
    return sb


def test_terminal_resolves_the_sidecar_container(monkeypatch, tmp_path):
    from olano.runtime import terminal as term

    _live_sidecar(monkeypatch, tmp_path)
    assert term.sandbox_container_ref("rw") == FAKE_CONTAINER


def test_terminal_close_reason_names_the_recorded_outcome(monkeypatch, tmp_path):
    from olano.runtime import terminal as term

    assert term.sandbox_unavailable_reason("rw") == "sandbox not running"  # never booted
    builder._record_sandbox_status(
        "rw", builder.SANDBOX_START_FAILED, "sandbox container could not start: no image"
    )
    assert term.sandbox_unavailable_reason("rw") == "sandbox container could not start: no image"


def test_terminal_close_reason_fits_a_websocket_close_frame():
    from olano.runtime import terminal as term

    builder._record_sandbox_status("rw", builder.SANDBOX_START_FAILED, "é" * 200)
    reason = term.sandbox_unavailable_reason("rw")
    assert len(reason.encode("utf-8")) <= 123
    assert reason.endswith("…")


def test_sandboxed_run_routes_an_in_process_tool_into_the_sidecar(monkeypatch, tmp_path):
    """The security half: ffmpeg/gcloud from a Read & Write agent used to run
    on the runtime host on a managed box. Now they run in its sidecar."""
    from olano.tools._sandbox_exec import sandbox_active_for_current_agent, sandboxed_run
    from olano.tools.talk_to import current_agent

    rec: dict = {}
    _live_sidecar(monkeypatch, tmp_path, rec)
    tok = current_agent.set("rw")
    try:
        assert sandbox_active_for_current_agent() is True
        r = sandboxed_run(["ffmpeg", "-version"], timeout=5)
    finally:
        current_agent.reset(tok)
    assert r.stdout == "SIDECAR_OUT" and rec["cmd"] == "ffmpeg -version"


def test_run_agent_command_uses_the_sidecar_on_a_managed_box(monkeypatch, tmp_path):
    from olano.admin import agent_exec

    rec: dict = {}
    _live_sidecar(monkeypatch, tmp_path, rec)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(agent_exec, "_workspace_root", lambda agent: ws)
    receipt = agent_exec.run_agent_command("rw", "ls")
    assert receipt["executor"] == "sandbox" and receipt["stdout"] == "SIDECAR_OUT"


def test_diagnostics_report_the_sidecar(monkeypatch, tmp_path):
    from olano.admin import diagnostics
    from olano.config import loader

    cfg = _cfg(FilesystemBackendConfig())
    monkeypatch.setattr(loader, "load_agent_config", lambda name, **k: cfg)

    assert diagnostics.sandbox_report("rw") == {"configured": False}  # never started

    _live_sidecar(monkeypatch, tmp_path)
    report = diagnostics.sandbox_report("rw")
    assert report["configured"] is True
    assert report["role"] == "sidecar"
    assert report["backend_type"] == "filesystem"
    assert report["started"] is True and report["container"] == FAKE_CONTAINER
