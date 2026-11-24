"""One path namespace for sandboxed agents: host paths stay valid in-container.

The model only ever receives host paths — the prompt advertises the host
workspace root, the security middleware normalizes every file-tool argument to
a host-absolute path, and deepagents' skills scanner is handed host dirs whose
SKILL.md locations it advertises verbatim. The sandbox therefore mounts each of
those trees at its own host path (workspace rw, skill roots ro, shared sources
at both `/shared/<name>` and their host path), keeping `/workspace` as an
alias. Without this, a sandboxed agent's first `ls` of its prompt-advertised
workspace answers "path_not_found" — the production report from a managed box.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import olano.core.builder as builder
from olano.backends.lionclaw_docker import _TOOL_LAYER_DIR, OlanoDockerBackend
from olano.config.agent_config import AgentConfig, SharedPathConfig
from olano.config.backend_config import OlanoDockerBackendConfig


def _mount_args(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == "-v"]


# ── backend: the workspace is mounted at its host path too ────────────────────


def test_run_args_mount_workspace_at_both_paths(tmp_path):
    b = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))
    b._workspace_root = tmp_path

    mounts = _mount_args(b._base_run_args())

    ws = str(tmp_path.resolve())
    assert f"{ws}:/workspace:rw" in mounts
    assert f"{ws}:{ws}:rw" in mounts


def test_run_args_skip_identical_mount_when_target_already_claimed(tmp_path):
    """An extra mount that already claims the workspace's host path must win —
    docker rejects a container path mounted twice."""
    b = OlanoDockerBackend(OlanoDockerBackendConfig(persistent=True))
    b._workspace_root = tmp_path
    ws = str(tmp_path.resolve())
    b._extra_mounts = [("/elsewhere", ws, "ro")]

    mounts = _mount_args(b._base_run_args())

    assert f"{ws}:{ws}:rw" not in mounts
    assert mounts.count(f"/elsewhere:{ws}:ro") == 1


def test_container_signature_carries_a_schema_bump(tmp_path):
    """Persistent containers made by pre-namespace code lack the identical-path
    mounts; the signature must not match them, or start() reattaches to a
    container where every prompt-derived path still fails."""
    import hashlib
    import json

    from olano.backends.lionclaw_docker import _container_signature

    sig = _container_signature(
        image="img",
        network="bridge",
        workspace_root=tmp_path,
        extra_mounts=[],
        memory="512m",
        cpus=0.5,
    )
    base_payload = {
        "image": "img",
        "network": "bridge",
        "workspace_root": str(tmp_path.resolve()),
        "mounts": [],
    }

    def _hash(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    # Schema 1 = pre-namespace (no identical-path workspace mount).
    assert sig != _hash(base_payload)
    # Schema 2 = pre-toolkit image: `image` is a tag string, never a digest, so
    # re-pulling a rebuilt olano-sandbox:latest does NOT move this hash on its
    # own. Schema 3 is what makes containers built before `python`/`zstd` were
    # baked in get recreated instead of silently reattached to.
    assert sig != _hash({"schema": 2, **base_payload})


def test_container_signature_tracks_the_resource_limits(tmp_path):
    """Docker cannot retune a running container's caps, so a changed limit has
    to recreate the container — hashing the values is what makes that automatic
    (no schema bump needed for a limit change)."""
    from olano.backends.lionclaw_docker import _container_signature

    common = {
        "image": "img",
        "network": "bridge",
        "workspace_root": tmp_path,
        "extra_mounts": [],
    }
    base = _container_signature(memory="512m", cpus=0.5, **common)

    assert base == _container_signature(memory="512m", cpus=0.5, **common), "must be stable"
    assert base != _container_signature(memory="4g", cpus=0.5, **common)
    assert base != _container_signature(memory="512m", cpus=2.0, **common)


# ── builder: skill roots + shared sources land at their host paths ────────────


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _targets(mounts: list[tuple[str, str, str]]) -> dict[str, tuple[str, str]]:
    return {container: (host, mode) for host, container, mode in mounts}


def test_managed_skills_dir_is_mounted_read_only_at_its_host_path(home, tmp_path):
    skills = home / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()

    mounts = builder._resolve_sandbox_shared_mounts(AgentConfig(name="a", system_prompt="x"), ws)

    by_target = _targets(mounts)
    skills_root = str((home / "skills").resolve())
    assert by_target[skills_root] == (skills_root, "ro")


def test_workspace_resident_skill_dirs_get_no_extra_mount(home, tmp_path):
    """<ws>/skills and <ws>/skills/learned ride the workspace mount."""
    ws = tmp_path / "ws"
    (ws / "skills" / "learned").mkdir(parents=True)

    mounts = builder._resolve_sandbox_shared_mounts(AgentConfig(name="a", system_prompt="x"), ws)

    ws_str = str(ws.resolve())
    assert not any(container.startswith(ws_str) for _h, container, _m in mounts)


def test_shared_sources_are_mounted_at_target_and_host_path(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    mounts = builder._resolve_sandbox_shared_mounts(AgentConfig(name="a", system_prompt="x"), ws)

    by_target = _targets(mounts)
    shared_src = str((home / "shared").resolve())
    assert by_target["/shared/default"] == (shared_src, "rw")
    # The workspace shared/default symlink points at the host source — it only
    # resolves in-container because the source is mounted there too.
    assert by_target[shared_src] == (shared_src, "rw")


def test_custom_shared_path_gets_both_mounts(home, tmp_path):
    extra_src = home / "shared" / "handbook"
    extra_src.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = AgentConfig(
        name="a",
        system_prompt="x",
        shared_paths=[
            SharedPathConfig(id="hb", source=str(extra_src), target="/shared/handbook", mode="ro")
        ],
    )

    mounts = builder._resolve_sandbox_shared_mounts(cfg, ws)

    by_target = _targets(mounts)
    src = str(extra_src.resolve())
    assert by_target["/shared/handbook"] == (src, "ro")
    assert by_target[src] == (src, "ro")


def test_container_targets_are_never_duplicated(home, tmp_path):
    """Two grants for the same source, plus the tool layer + skills — every
    container path appears exactly once (docker errors on duplicates)."""
    skills = home / "skills"
    skills.mkdir(parents=True)
    src = home / "shared" / "common"
    src.mkdir(parents=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = AgentConfig(
        name="a",
        system_prompt="x",
        shared_paths=[
            SharedPathConfig(id="one", source=str(src), target="/shared/one", mode="rw"),
            SharedPathConfig(id="two", source=str(src), target="/shared/two", mode="ro"),
        ],
    )

    mounts = builder._resolve_sandbox_shared_mounts(cfg, ws)

    targets = [container for _h, container, _m in mounts]
    assert len(targets) == len(set(targets))
    assert _TOOL_LAYER_DIR in targets


def test_home_shared_rw_source_is_handed_to_the_sandbox_user(home, tmp_path, monkeypatch):
    """The home scaffold creates <home>/shared as root; a bind mount preserves
    that, so the default rw grant must re-own it or no agent can write the
    common folder (verified live: "permission denied" creating notes.md)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    ensured: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        builder,
        "_ensure_sandbox_shared_dir",
        lambda p, recursive=False: ensured.append((Path(p), recursive)),
    )

    builder._resolve_sandbox_shared_mounts(AgentConfig(name="a", system_prompt="x"), ws)

    shared_src = (home / "shared").resolve()
    assert (shared_src, True) in [(p.resolve(), rec) for p, rec in ensured]


def test_disallowed_shared_source_gets_no_identical_mount_either(home, tmp_path):
    """A denied source (e.g. the home root) must not sneak in via the
    path-identical lane."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = AgentConfig(
        name="a",
        system_prompt="x",
        shared_paths=[
            SharedPathConfig(id="evil", source=str(home), target="/shared/home", mode="rw")
        ],
    )
    home.mkdir(parents=True, exist_ok=True)

    mounts = builder._resolve_sandbox_shared_mounts(cfg, ws)

    assert "/shared/home" not in _targets(mounts)
    assert str(home.resolve()) not in _targets(mounts)


# ── prompt: the workspace block tells sandboxed agents the truth ──────────────


def test_prompt_mentions_the_sandbox_alias_for_sandboxed_agents(monkeypatch, tmp_path):
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = AgentConfig(name="sandboxed", system_prompt="x")
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, "sandboxed", object())

    block = PromptAssembler(cfg, Workspace(root=ws))._build_workspace_environment()

    assert f"`{ws.resolve()}`" in block
    assert "isolated sandbox container" in block
    assert "`/workspace`" in block
    assert "/shared/<name>" in block
    # The layout rules are shared by both variants.
    assert "`reports/` — every file you produce for people" in block


def test_prompt_keeps_the_legacy_wording_for_host_backends(tmp_path):
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = AgentConfig(name="plain", system_prompt="x")

    block = PromptAssembler(cfg, Workspace(root=ws))._build_workspace_environment()

    assert "maps to this same root for container-style paths" in block
    assert "isolated sandbox container" not in block
    assert "`reports/` — every file you produce for people" in block


def test_prompt_treats_explicit_docker_backend_as_sandboxed(tmp_path):
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = AgentConfig(name="explicit", system_prompt="x", backend=OlanoDockerBackendConfig())

    block = PromptAssembler(cfg, Workspace(root=ws))._build_workspace_environment()

    assert "isolated sandbox container" in block
