"""A cognition worker runs in its parent's sandbox, never in one of its own.

The production reading these pin (one managed box, 2026-10-11): the update
agent's census said `docker0=2/1024 orphans=0 top=br-4b179921670b:959
SATURATED veth=964 ctrs=969 mcp=0 sbx=960 stopped=5` on a box with twelve
agents. The stdio-MCP family the reaper bounds was at zero; the 960 were
*persistent agent sandboxes* (`lionclaw.signature`), one per background run:
`_build_worker_config` gives every cognition / `delegate_task` run a fresh
unique agent name (`<parent>__cog__<label>__<hex>`), the builder keyed the
persistent container on that name, and nothing ever stopped it.

`builder.sandbox_owner(config)` is the fix: `credential_scope or name`, the
seam that already makes a worker resolve credentials as its parent. A worker is
handed the parent's live backend when the parent is in the registry, otherwise
its container is started under the PARENT's name (the one the parent's own
next build looks up), and `release_agent_sandbox` forgets the child's registry
rows when the run ends.
"""

from __future__ import annotations

import pytest

import olano.core.builder as builder
from olano.backends.lionclaw_docker import OlanoDockerBackend, _persistent_container_name
from olano.cognition.worker import _build_worker_config
from olano.config.agent_config import AgentConfig
from olano.config.backend_config import FilesystemBackendConfig, LocalShellBackendConfig

PARENT = "merli"


def _posture(monkeypatch, *, shell=True, managed=True, sandbox=True):
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: shell)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: managed)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: sandbox)


def _startable(monkeypatch) -> list[str]:
    """Run the real start path without docker; returns the names start() was given."""
    started: list[str] = []

    def fake_start(self, *, agent_name, workspace_root, shared_mounts=None):
        started.append(agent_name)
        self._agent_name = agent_name
        self._workspace_root = workspace_root
        self._container_name = _persistent_container_name(agent_name, workspace_root)

    monkeypatch.setattr(OlanoDockerBackend, "start", fake_start)
    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda *a, **k: None)
    monkeypatch.setattr(builder, "_seed_sandbox_tool_sets", lambda backend: None)
    monkeypatch.setattr(builder, "_resolve_sandbox_shared_mounts", lambda cfg, ws: [])
    return started


def _parent(backend) -> AgentConfig:
    return AgentConfig(name=PARENT, system_prompt="x", backend=backend)


def _worker(parent: AgentConfig, *, want_shell: bool = False) -> AgentConfig:
    return _build_worker_config(
        parent,
        system_prompt="s",
        tools_allow=None,
        extra_blocked=None,
        model=None,
        label="pulse_agent",
        want_shell=want_shell,
    )


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(builder, "_SANDBOX_BACKENDS", {})
    monkeypatch.setattr(builder, "_SANDBOX_STATUS", {})


# ── the owner seam ────────────────────────────────────────────────────────────


def test_sandbox_owner_is_the_agent_itself_or_the_workers_parent():
    parent = _parent(FilesystemBackendConfig(root_dir="workspace"))
    assert builder.sandbox_owner(parent) == PARENT
    child = _worker(parent)
    assert child.name != PARENT and "__cog__" in child.name
    assert builder.sandbox_owner(child) == PARENT
    # Chain-safe: a worker's worker still points at the top-level agent.
    grandchild = _worker(child)
    assert builder.sandbox_owner(grandchild) == PARENT


def test_owner_kwarg_is_absent_for_an_agent_that_owns_its_sandbox():
    """The common path reaches `_try_build_sandbox_backend` with the arguments
    it always had, so nothing that fakes it (tests, tooling) changes."""
    assert builder._owner_kwarg(PARENT, PARENT) == {}
    assert builder._owner_kwarg(PARENT, None) == {}
    assert builder._owner_kwarg(None, PARENT) == {}
    assert builder._owner_kwarg("merli__cog__x__ab12cd", PARENT) == {"sandbox_owner": PARENT}


# ── a worker shares the live parent's container ───────────────────────────────


def test_worker_with_shell_borrows_the_parents_live_sandbox(monkeypatch, tmp_path):
    _posture(monkeypatch)
    started = _startable(monkeypatch)
    parent = _parent(LocalShellBackendConfig(root_dir="workspace"))

    parent_backend = builder._build_deepagents_backend(
        parent.backend,
        tmp_path,
        agent_name=parent.name,
        sandbox_owner=builder.sandbox_owner(parent),
    )
    assert isinstance(parent_backend, OlanoDockerBackend)
    assert started == [PARENT]

    child = _worker(parent)  # inherits the parent's local_shell backend
    child_backend = builder._build_deepagents_backend(
        child.backend,
        tmp_path,
        agent_name=child.name,
        sandbox_owner=builder.sandbox_owner(child),
    )

    assert child_backend is parent_backend  # the very same object, no new container
    assert started == [PARENT]  # start() ran once, for the parent
    # The worker's own turn (`current_agent` is the child name) still reaches it.
    assert builder.get_sandbox_backend(child.name) is parent_backend
    status = builder.sandbox_status(child.name)
    assert status["state"] == builder.SANDBOX_STARTED
    assert status["container"] == parent_backend.container_ref()
    assert PARENT in status["reason"]


def test_worker_promoted_to_shell_borrows_the_parents_sidecar(monkeypatch, tmp_path):
    """A read/write parent has a sidecar; `want_shell` promotes the worker's
    backend to local_shell, whose sandbox must be that sidecar, not a sibling."""
    _posture(monkeypatch)
    started = _startable(monkeypatch)
    parent = _parent(FilesystemBackendConfig(root_dir="workspace", access_mode="read_write"))

    sidecar = builder._start_sidecar_sandbox(parent, tmp_path)
    assert sidecar is not None and started == [PARENT]

    child = _worker(parent, want_shell=True)
    assert isinstance(child.backend, LocalShellBackendConfig)
    child_backend = builder._build_deepagents_backend(
        child.backend,
        tmp_path,
        agent_name=child.name,
        sandbox_owner=builder.sandbox_owner(child),
    )
    assert child_backend is sidecar
    assert started == [PARENT]
    # For the worker the container IS its backend (it has `execute`), whatever
    # role the parent recorded for the same container.
    assert builder.sandbox_role(child.name) == builder.SANDBOX_ROLE_BACKEND
    assert builder.sandbox_role(PARENT) == builder.SANDBOX_ROLE_SIDECAR


def test_worker_sidecar_borrows_too(monkeypatch, tmp_path):
    """A worker without shell (plain filesystem backend) gets a sidecar on a
    managed box like any agent; that sidecar is the parent's container."""
    _posture(monkeypatch)
    started = _startable(monkeypatch)
    parent = _parent(FilesystemBackendConfig(root_dir="workspace"))
    parent_sidecar = builder._start_sidecar_sandbox(parent, tmp_path)

    child = _worker(parent)
    child_sidecar = builder._start_sidecar_sandbox(child, tmp_path)

    assert child_sidecar is parent_sidecar
    assert started == [PARENT]
    assert builder.sandbox_role(child.name) == builder.SANDBOX_ROLE_SIDECAR


# ── no live parent: the container is still the parent's ───────────────────────


def test_worker_without_a_live_parent_starts_the_parents_container(monkeypatch, tmp_path):
    """Nothing registered for the parent (a one-shot process, a parent whose
    build failed): the worker starts a sandbox, but under the PARENT's name —
    so the parent's own next build reattaches to it by name instead of a
    `lionclaw-merli__cog__…` container nobody will ever look up again."""
    _posture(monkeypatch)
    started = _startable(monkeypatch)
    parent = _parent(LocalShellBackendConfig(root_dir="workspace"))
    child = _worker(parent)

    child_backend = builder._build_deepagents_backend(
        child.backend,
        tmp_path,
        agent_name=child.name,
        sandbox_owner=builder.sandbox_owner(child),
    )

    assert isinstance(child_backend, OlanoDockerBackend)
    assert started == [PARENT]
    assert child_backend.container_ref() == _persistent_container_name(PARENT, tmp_path)
    assert "__cog__" not in child_backend.container_ref()
    # Registered under the worker's name (its turn looks it up), not the parent's:
    # the parent registers itself when it builds.
    assert builder.get_sandbox_backend(child.name) is child_backend
    assert builder.get_sandbox_backend(PARENT) is None


def test_agent_without_owner_still_starts_under_its_own_name(monkeypatch, tmp_path):
    """The regression guard for every ordinary agent: no owner kwarg, same
    container name as before."""
    _posture(monkeypatch)
    started = _startable(monkeypatch)
    parent = _parent(LocalShellBackendConfig(root_dir="workspace"))
    backend = builder._build_deepagents_backend(parent.backend, tmp_path, agent_name=PARENT)
    assert started == [PARENT]
    assert backend.container_ref() == _persistent_container_name(PARENT, tmp_path)


# ── the registry is released when the run ends ────────────────────────────────


def test_release_agent_sandbox_forgets_the_child_and_keeps_the_parent(monkeypatch, tmp_path):
    _posture(monkeypatch)
    _startable(monkeypatch)
    parent = _parent(LocalShellBackendConfig(root_dir="workspace"))
    parent_backend = builder._build_deepagents_backend(
        parent.backend, tmp_path, agent_name=PARENT, sandbox_owner=PARENT
    )
    child = _worker(parent)
    builder._build_deepagents_backend(
        child.backend, tmp_path, agent_name=child.name, sandbox_owner=PARENT
    )
    assert builder.get_sandbox_backend(child.name) is parent_backend

    builder.release_agent_sandbox(child.name)

    assert builder.get_sandbox_backend(child.name) is None
    assert builder.sandbox_status(child.name) is None
    assert builder.get_sandbox_backend(PARENT) is parent_backend  # untouched
    builder.release_agent_sandbox("never-registered")  # a no-op, never an error
    builder.release_agent_sandbox(None)


async def test_run_worker_releases_its_sandbox_registration(base_config, tmp_path, monkeypatch):
    """End to end through `run_worker`: a build that registered a sandbox under
    the child's name leaves nothing behind, on the failure path included."""
    from olano.cognition import worker as worker_mod

    seen: list[str] = []

    async def fake_build(config, **kwargs):
        seen.append(config.name)
        builder._SANDBOX_BACKENDS[config.name] = object()
        builder._record_sandbox_status(config.name, builder.SANDBOX_STARTED, "fake")
        raise RuntimeError("boom after the backend phase")

    monkeypatch.setattr(builder, "build_agent_async", fake_build)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")  # past the model-ready gate

    outcome = await worker_mod.run_worker(
        parent_config=base_config,
        workspace_root=tmp_path,
        playbook="do the thing",
        goal="g",
        budget_check=False,
        notify_operator=False,
    )

    assert outcome.status == "error"
    assert len(seen) == 1 and "__cog__" in seen[0]
    assert builder.get_sandbox_backend(seen[0]) is None
    assert builder.sandbox_status(seen[0]) is None
