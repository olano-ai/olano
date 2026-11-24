"""Environment × posture safety matrix for managed deployments.

The source-safety invariant on a managed box: NO agent environment may (a) give
a raw shell on the runtime container (which holds /app + the vault), or (b) let
a customer read/write outside the workspace + designated shared areas. This
suite pins that invariant across every backend and the adversarial edges the
first pass missed (resolver-error fail-open, shared_paths → secret re-mount).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import olano.core.builder as builder
from olano.config.agent_config import AgentConfig, SharedPathConfig
from olano.config.backend_config import (
    FilesystemBackendConfig,
    LocalShellBackendConfig,
    StateBackendConfig,
    StoreBackendConfig,
)


def _t(obj) -> str:
    return type(obj).__name__


def _managed(monkeypatch, *, shell=True, sandbox=False):
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: shell)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: sandbox)


def _open(monkeypatch):
    import olano.deployment.enforcement as enf

    monkeypatch.setattr(enf, "feature_enabled", lambda name: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: False)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)


# ── The one dangerous environment: local_shell (Read/Write/Execute) ──────────


def test_open_local_shell_gets_real_host_shell(monkeypatch, tmp_path):
    _open(monkeypatch)
    b = builder._build_deepagents_backend(LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path)
    assert _t(b) == "LocalShellBackend"  # operator's own box — unaffected


def test_managed_local_shell_no_sandbox_is_no_shell(monkeypatch, tmp_path):
    _managed(monkeypatch, sandbox=False)
    b = builder._build_deepagents_backend(LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path)
    assert _t(b) == "FilesystemBackend"  # no host shell, ever


def test_managed_local_shell_sandbox_fails_is_no_shell_not_host(monkeypatch, tmp_path):
    _managed(monkeypatch, sandbox=True)
    monkeypatch.setattr(builder, "_try_build_sandbox_backend", lambda *a, **k: None)
    b = builder._build_deepagents_backend(LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path)
    assert _t(b) == "FilesystemBackend"  # fail CLOSED, never LocalShellBackend


def test_managed_local_shell_sandbox_ok_routes_to_docker(monkeypatch, tmp_path):
    _managed(monkeypatch, sandbox=True)
    sentinel = object()
    monkeypatch.setattr(builder, "_try_build_sandbox_backend", lambda *a, **k: sentinel)
    b = builder._build_deepagents_backend(LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path)
    assert b is sentinel  # transparently the docker sandbox


# ── NEW: resolver ERROR must fail closed on a managed box (was fail-OPEN) ─────


def test_resolver_error_on_managed_box_fails_closed(monkeypatch, tmp_path):
    import olano.deployment.enforcement as enf

    def boom(*a, **k):
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(enf, "feature_enabled", boom)
    monkeypatch.setattr(builder, "_managed_install_fallback", lambda: True)  # manifest present
    b = builder._build_deepagents_backend(LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path)
    assert _t(b) == "FilesystemBackend"  # NOT a host shell


def test_resolver_error_on_unmanaged_box_keeps_shell(monkeypatch, tmp_path):
    import olano.deployment.enforcement as enf

    def boom(*a, **k):
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(enf, "feature_enabled", boom)
    monkeypatch.setattr(builder, "_managed_install_fallback", lambda: False)  # no manifest
    b = builder._build_deepagents_backend(LocalShellBackendConfig(root_dir=str(tmp_path)), tmp_path)
    assert _t(b) == "LocalShellBackend"  # operator box unaffected by the error


def test_managed_install_fallback_reads_manifest(monkeypatch, tmp_path):
    import olano.deployment.sources as sources

    present = tmp_path / "installation.json"
    present.write_text("{}")
    monkeypatch.setattr(sources, "manifest_path", lambda: present)
    assert builder._managed_install_fallback() is True

    monkeypatch.setattr(sources, "manifest_path", lambda: tmp_path / "nope.json")
    assert builder._managed_install_fallback() is False


# ── The safe environments behave identically under both postures ─────────────


@pytest.mark.parametrize(
    "cfg,expected",
    [
        (StateBackendConfig(), "StateBackend"),
        (StoreBackendConfig(namespace=["x"]), "StoreBackend"),
        (
            FilesystemBackendConfig(root_dir="workspace", access_mode="read_write"),
            "FilesystemBackend",
        ),
        (FilesystemBackendConfig(root_dir="workspace", access_mode="read"), "FilesystemBackend"),
    ],
)
def test_non_shell_environments_are_posture_independent(monkeypatch, tmp_path, cfg, expected):
    _managed(monkeypatch, sandbox=False)
    assert _t(builder._build_deepagents_backend(cfg, tmp_path)) == expected


def test_read_only_filesystem_denies_writes(tmp_path):
    cfg = AgentConfig(
        name="ro",
        system_prompt="x",
        backend=FilesystemBackendConfig(root_dir="workspace", access_mode="read"),
    )
    perms = builder._build_permissions(cfg, tmp_path)
    # A deny-write FilesystemPermission is produced for read-only workspaces.
    assert perms, "read-only backend must produce a permission"
    assert any(
        getattr(p, "mode", None) == "deny" and "write" in (getattr(p, "operations", []) or [])
        for p in perms
    )


# ── NEW: shared_paths cannot re-mount secrets/source into the sandbox ─────────


@pytest.mark.parametrize("rel", ["vault", "vault.db"])
def test_shared_source_denies_vault(monkeypatch, tmp_path, rel):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    assert builder._sandbox_shared_source_allowed(tmp_path / rel) is False


@pytest.mark.parametrize("p", ["/opt/olano", "/app", "/app/olano/core/builder.py", "/etc/passwd"])
def test_shared_source_denies_host_secrets_and_source(monkeypatch, tmp_path, p):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    assert builder._sandbox_shared_source_allowed(Path(p)) is False


def test_shared_source_denies_home_root_but_allows_shared(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    assert builder._sandbox_shared_source_allowed(tmp_path) is False  # whole home
    assert builder._sandbox_shared_source_allowed(tmp_path / "shared") is True
    assert builder._sandbox_shared_source_allowed(tmp_path / "shared" / "data") is True


def test_shared_source_denies_arbitrary_host_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    assert builder._sandbox_shared_source_allowed(Path("/tmp/some-random-dir")) is False


def test_resolve_sandbox_mounts_excludes_vault_keeps_safe(monkeypatch, tmp_path):
    """End-to-end: a config whose shared_paths point at BOTH the vault and a safe
    shared dir yields a sandbox mount list with the safe dir + tool layer, and
    NEVER the vault."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    (vault / "vault.db").write_text("SECRET")
    safe = tmp_path / "shared" / "data"
    safe.mkdir(parents=True)

    cfg = AgentConfig(
        name="a",
        system_prompt="x",
        shared_paths=[
            SharedPathConfig(source=str(vault), target="v"),
            SharedPathConfig(source=str(safe), target="d"),
        ],
    )
    mounts = builder._resolve_sandbox_shared_mounts(cfg, tmp_path / "ws")
    hosts = [h for h, _c, _m in mounts]
    assert str(vault.resolve()) not in hosts  # the vault is refused
    assert str(safe.resolve()) in hosts  # the safe shared dir is mounted
    assert any(c == "/opt/agent-tools" for _h, c, _m in mounts)  # tool layer present


# ── NEW: the two functional bugs the first (chmod-0777 / --user) test masked ──


def test_sandbox_build_chowns_workspace_for_uid_1000(monkeypatch, tmp_path):
    """The runtime creates AND seeds the workspace as root; the sandbox runs as
    uid 1000. The build must re-own the whole tree — chowning only the top dir
    leaves the agent able to create new files but not to edit, delete, or write
    inside anything the runtime seeded (MEMORY.md, memories/, system/…)."""
    from olano.backends.lionclaw_docker import OlanoDockerBackend

    ensured: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        builder,
        "_ensure_sandbox_shared_dir",
        lambda p, recursive=False: ensured.append((Path(p), recursive)),
    )
    monkeypatch.setattr(OlanoDockerBackend, "start", lambda self, **k: None)

    ws = tmp_path / "agents" / "a" / "workspace"
    b = builder._try_build_sandbox_backend(
        LocalShellBackendConfig(root_dir=str(ws)),
        workspace_root=ws,
        agent_name="a",
        shared_mounts=[],
    )
    assert b is not None
    recursive_for = {p.resolve() for p, rec in ensured if rec}
    assert ws.resolve() in recursive_for


def test_sandbox_build_stamps_the_resolved_resource_limits(monkeypatch, tmp_path):
    """Limits are resolved in the builder, not the backend, so the backend keeps
    consuming concrete values. The per-agent override has to reach the resolver,
    or an agent's own `sandbox:` block is silently ignored."""
    from olano.backends.lionclaw_docker import OlanoDockerBackend
    from olano.config.agent_config import SandboxResourceOverride
    from olano.core.host_resources import HostResources, SandboxLimits

    monkeypatch.setattr(builder, "_ensure_sandbox_shared_dir", lambda p, recursive=False: None)
    monkeypatch.setattr(OlanoDockerBackend, "start", lambda self, **k: None)
    monkeypatch.setattr(builder, "_seed_sandbox_tool_sets", lambda backend: None)

    seen: list[tuple[str | None, float | None]] = []

    def fake_resolve(memory=None, cpus=None, **kwargs):
        seen.append((memory, cpus))
        return SandboxLimits(
            memory="4g",
            cpus=2.0,
            memory_source="agent",
            cpus_source="auto",
            host=HostResources(ncpu=4, mem_bytes=16 * 1024**3, source="docker_info"),
        )

    monkeypatch.setattr("olano.core.host_resources.resolve_sandbox_limits", fake_resolve)

    ws = tmp_path / "agents" / "a" / "workspace"
    b = builder._try_build_sandbox_backend(
        LocalShellBackendConfig(root_dir=str(ws)),
        workspace_root=ws,
        agent_name="a",
        shared_mounts=[],
        sandbox_overrides=SandboxResourceOverride(memory="4g"),
    )
    assert b is not None
    assert seen == [("4g", None)], "the agent's own override must reach the resolver"
    assert b._config.memory == "4g"
    assert b._config.cpus == 2.0


def test_pip_user_enabled_so_bare_pip_install_works():
    from olano.backends.lionclaw_docker import _TOOL_LAYER_ENV

    # Without PIP_USER, a bare `pip install X` targets root-owned system
    # site-packages and fails for the uid-1000 sandbox user.
    assert _TOOL_LAYER_ENV.get("PIP_USER") == "1"
    assert _TOOL_LAYER_ENV.get("PYTHONUSERBASE") == "/opt/agent-tools/python"
