"""In-process tools + composite backends route command execution into the sandbox.

Pins: `sandboxed_run` runs a command in the agent's live sandbox when present
(docker exec) and locally otherwise; the gcloud toolkit never shells out at all
(class 4 — an honest `[unsupported]`, no credential in any sandbox); and a live sandbox
nested inside a CompositeBackend is recognized + used (so a composite agent with
a shell route runs `execute` in the container instead of being refused).
"""

from __future__ import annotations

import olano.core.builder as builder
from olano.backends.lionclaw_docker import OlanoDockerBackend, SandboxRun
from olano.config.backend_config import OlanoDockerBackendConfig
from olano.security.deepagents_middleware import (
    _extract_live_sandbox,
    _is_live_sandbox_backend,
)
from olano.tools._sandbox_exec import sandbox_active_for_current_agent, sandboxed_run
from olano.tools.talk_to import current_agent


def _fake_sandbox(monkeypatch, recorder):
    sb = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))
    # A backend only counts as live once start() has bound it to a container;
    # bind one by hand rather than starting docker.
    sb._container_name = "olano-test-container"

    def fake_run(command, **kw):
        recorder["cmd"] = command
        return SandboxRun("SANDBOX_OUT", "", 0)

    monkeypatch.setattr(sb, "run", fake_run)
    return sb


def test_sandboxed_run_routes_to_live_sandbox(monkeypatch):
    rec: dict = {}
    sb = _fake_sandbox(monkeypatch, rec)
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, "alpha", sb)
    tok = current_agent.set("alpha")
    try:
        assert sandbox_active_for_current_agent() is True
        r = sandboxed_run(["gcloud", "projects", "list"], timeout=10)
    finally:
        current_agent.reset(tok)
    assert r.returncode == 0 and r.stdout == "SANDBOX_OUT"
    assert rec["cmd"] == "gcloud projects list"  # shlex.join of the argv


def test_sandboxed_run_falls_back_to_host_when_no_sandbox():
    tok = current_agent.set("no-such-agent-xyz")
    try:
        assert sandbox_active_for_current_agent() is False
        r = sandboxed_run(["echo", "hello-host"], timeout=10)
    finally:
        current_agent.reset(tok)
    assert r.returncode == 0 and "hello-host" in r.stdout


def test_gcloud_tools_are_unsupported_and_never_shell_out(monkeypatch):
    """gcloud is class 4: its auth is a service-account file, which would have
    to live inside the agent's sandbox — Olano never provides that. The tools
    keep their names (old configs still load) and answer honestly, without
    ever spawning a process."""
    import subprocess

    import olano.tools.gcloud_tools as g

    def never(*a, **kw):
        raise AssertionError("gcloud tools must not spawn anything")

    monkeypatch.setattr(subprocess, "run", never)
    monkeypatch.setattr(subprocess, "Popen", never)
    assert not hasattr(g, "sandboxed_run")

    for call in (
        lambda: g._gcloud(["projects", "list"]),
        lambda: g.gcloud_run_command.invoke({"command": "compute instances list"}),
        lambda: g.gcloud_deploy.invoke({"service_name": "svc", "image": "gcr.io/p/i:t"}),
        lambda: g.gcloud_project_info.invoke({}),
    ):
        out = call()
        assert out.startswith("[unsupported]") or "[unsupported]" in out
        assert "gcloud CLI is not part of the supported toolkit" in out
        assert "Google toolkit or an MCP server" in out
    names = {t.name for t in g.get_tools()}
    assert {"gcloud_run_command", "gcloud_deploy", "gcloud_storage_upload"} <= names


# ── Composite-backend recognition (was: fails safe = no shell) ────────────────


class _FakeComposite:
    """Stand-in for deepagents CompositeBackend (default + routes attrs)."""

    def __init__(self, default=None, routes=None):
        self.default = default
        self.routes = routes or {}


def test_extract_live_sandbox_direct_and_composite(monkeypatch):
    sb = OlanoDockerBackend(OlanoDockerBackendConfig())
    sb._container_name = "olano-test-container"  # liveness needs a container

    # direct
    assert _extract_live_sandbox(sb) is sb
    assert _is_live_sandbox_backend(sb)

    # nested as the composite default
    assert _extract_live_sandbox(_FakeComposite(default=sb)) is sb
    assert _is_live_sandbox_backend(_FakeComposite(default=sb))

    # nested as a composite route
    assert _extract_live_sandbox(_FakeComposite(default=object(), routes={"/x": sb})) is sb

    # no sandbox anywhere
    empty = _FakeComposite(default=object(), routes={"/x": object()})
    assert _extract_live_sandbox(empty) is None
    assert not _is_live_sandbox_backend(empty)
    assert _extract_live_sandbox(None) is None


def test_composite_recursion_threads_agent_name_and_mounts(monkeypatch, tmp_path):
    """A shell route inside a composite is coerced to a sandbox (agent_name +
    shared_mounts threaded), not silently dropped."""
    import deepagents.backends as db

    import olano.deployment.enforcement as enf
    from olano.config.agent_config import SandboxResourceOverride
    from olano.config.backend_config import (
        CompositeBackendConfig,
        LocalShellBackendConfig,
    )

    monkeypatch.setattr(enf, "feature_enabled", lambda n: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)
    monkeypatch.setattr(
        db, "CompositeBackend", lambda default, routes: ("composite", default, routes)
    )

    seen: dict = {}

    def fake_try(local_cfg, *, workspace_root, agent_name, shared_mounts, sandbox_overrides=None):
        seen["agent_name"] = agent_name
        seen["shared_mounts"] = shared_mounts
        seen["sandbox_overrides"] = sandbox_overrides
        return "SANDBOX"

    monkeypatch.setattr(builder, "_try_build_sandbox_backend", fake_try)

    overrides = SandboxResourceOverride(memory="4g")
    cfg = CompositeBackendConfig(default=LocalShellBackendConfig(root_dir="workspace"), routes={})
    result = builder._build_deepagents_backend(
        cfg,
        tmp_path,
        agent_name="alpha",
        shared_mounts=[("h", "c", "rw")],
        sandbox_overrides=overrides,
    )
    assert result == ("composite", "SANDBOX", {})
    assert seen["agent_name"] == "alpha"
    assert seen["shared_mounts"] == [("h", "c", "rw")]
    # A shell route nested in a composite must inherit the agent's own limits
    # too, or its sandbox silently runs on different caps than the default one.
    assert seen["sandbox_overrides"] is overrides


def test_an_unstarted_sandbox_is_not_live():
    """Traversal still finds it; liveness must not claim it.

    An unstarted backend used to pass `_is_live_sandbox_backend` on an
    isinstance check alone, so the managed fail-closed chokepoint waved it
    through and shell was routed into a container that did not exist.
    """
    from olano.security.deepagents_middleware import _extract_sandbox, _sandbox_started

    sb = OlanoDockerBackend(OlanoDockerBackendConfig())  # never started

    assert _extract_sandbox(sb) is sb  # traversal unchanged
    assert not _sandbox_started(sb)
    assert _extract_live_sandbox(sb) is None
    assert not _is_live_sandbox_backend(sb)

    # …and the same nested in a composite, either slot.
    assert _extract_sandbox(_FakeComposite(default=sb)) is sb
    assert not _is_live_sandbox_backend(_FakeComposite(default=sb))
    assert not _is_live_sandbox_backend(_FakeComposite(default=object(), routes={"/x": sb}))


def test_composite_docker_route_is_started_and_registered(monkeypatch, tmp_path):
    """A `lionclaw_docker` route inside a composite gets a real container too.

    The explicit-docker branch is reached recursively from CompositeBackendConfig,
    so it had the same never-started defect as a top-level docker backend.
    """
    import deepagents.backends as db

    import olano.backends.lionclaw_docker as ld
    import olano.deployment.enforcement as enf
    from olano.config.backend_config import CompositeBackendConfig, FilesystemBackendConfig

    monkeypatch.setattr(enf, "feature_enabled", lambda n: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)
    monkeypatch.setattr(
        db, "CompositeBackend", lambda default, routes: ("composite", default, routes)
    )
    monkeypatch.setattr(builder, "_seed_sandbox_tool_sets", lambda b: None)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda p, recursive=False: None)

    def fake_start(self, *, agent_name, workspace_root, shared_mounts=None):
        self._container_name = f"lionclaw-{agent_name}-test"

    monkeypatch.setattr(ld.OlanoDockerBackend, "start", fake_start)

    cfg = CompositeBackendConfig(
        default=FilesystemBackendConfig(root_dir="workspace"),
        routes={"/scratch": OlanoDockerBackendConfig(persistent=True)},
    )
    _kind, _default, routes = builder._build_deepagents_backend(
        cfg, tmp_path, agent_name="alpha", shared_mounts=[]
    )

    route = routes["/scratch"]
    assert isinstance(route, OlanoDockerBackend)
    assert route.container_ref() == "lionclaw-alpha-test"
    assert builder.get_sandbox_backend("alpha") is route
    assert _is_live_sandbox_backend(route)
