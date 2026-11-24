"""Live path-namespace scenarios — a production-shaped home, a real container.

Runs the full access matrix a deployment exercises, end to end, with the REAL
mount plan from ``builder._resolve_sandbox_shared_mounts``:

  * a normal agent using the prompt-advertised HOST workspace path
  * relative paths the way the security middleware normalizes them
  * managed skills (<home>/skills): deepagents' scanner via the backend,
    read access, and kernel-enforced read-only
  * the common shared folder at /shared/default, at its host path, and through
    the workspace `shared/` symlink projection
  * isolation: another agent's workspace stays invisible
  * upgrade: a stale pre-namespace container is recreated, not reattached

Same opt-in guard as test_sandbox_live.py: skips without Docker + the image.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

SANDBOX_IMAGE = os.environ.get("LIONCLAW_SANDBOX_IMAGE", "olano-sandbox:latest")


def _docker_ready() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
            return False, "no Docker daemon"
        probe = subprocess.run(
            ["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker unavailable: {exc}"
    if probe.returncode != 0:
        return False, f"sandbox image {SANDBOX_IMAGE!r} not present locally"
    return True, ""


_READY, _WHY = _docker_ready()
pytestmark = pytest.mark.skipif(not _READY, reason=f"live sandbox unavailable — {_WHY}")


@pytest.fixture(scope="module")
def deployment(tmp_path_factory):
    """A production-shaped LIONCLAW_HOME with two agents, skills, and shared."""
    home = tmp_path_factory.mktemp("home")
    prev_home = os.environ.get("LIONCLAW_HOME")
    os.environ["LIONCLAW_HOME"] = str(home)

    import olano.core.builder as builder
    from olano.backends.lionclaw_docker import OlanoDockerBackend
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import OlanoDockerBackendConfig

    # Managed skills, common shared dir, and two agents' workspaces — all
    # seeded by the runtime (this process), as in production.
    skill = home / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: live namespace demo\n---\n\nUse wisely.\n",
        encoding="utf-8",
    )
    (home / "shared").mkdir()
    (home / "shared" / "handbook.md").write_text("common knowledge\n", encoding="utf-8")

    ws = home / "agents" / "merli" / "workspace"
    (ws / "memories").mkdir(parents=True)
    (ws / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
    other_ws = home / "agents" / "colleague" / "workspace"
    other_ws.mkdir(parents=True)
    (other_ws / "PRIVATE.md").write_text("not yours\n", encoding="utf-8")

    config = AgentConfig(name="merli", system_prompt="x", workspace=str(ws))

    from olano.security.sharing import resolve_shared_mounts, sync_workspace_share_projections

    sync_workspace_share_projections(ws, resolve_shared_mounts(config))
    builder._ensure_sandbox_shared_dir(ws, recursive=True)

    mounts = builder._resolve_sandbox_shared_mounts(config, ws)
    backend = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="bridge", image=SANDBOX_IMAGE)
    )
    backend.start(agent_name="merli", workspace_root=ws, shared_mounts=mounts)

    yield backend, home, ws

    subprocess.run(
        ["docker", "rm", "-f", backend.container_ref() or ""], capture_output=True, timeout=60
    )
    if prev_home is None:
        os.environ.pop("LIONCLAW_HOME", None)
    else:
        os.environ["LIONCLAW_HOME"] = prev_home


def test_prompt_advertised_host_path_works_in_every_tool(deployment):
    """The exact failure from a managed box: `ls` on the host workspace path."""
    backend, _home, ws = deployment

    listing = backend.ls(str(ws))
    assert listing.error is None
    names = {e["path"].rsplit("/", 1)[-1] for e in listing.entries}
    assert "MEMORY.md" in names

    read = backend.read(str(ws / "MEMORY.md"))
    assert read.error is None
    assert "# memory" in read.file_data["content"]

    shell = backend.execute(f"cat {ws}/MEMORY.md && pwd")
    assert shell.exit_code == 0
    assert "# memory" in shell.output
    assert "/workspace" in shell.output  # the alias is still the working dir


def test_workspace_alias_and_host_path_are_the_same_directory(deployment):
    backend, _home, ws = deployment

    assert backend.write("/workspace/via-alias.txt", "hello\n").error is None
    read = backend.read(str(ws / "via-alias.txt"))
    assert read.error is None and "hello" in read.file_data["content"]
    assert (ws / "via-alias.txt").read_text(encoding="utf-8") == "hello\n"


def test_relative_paths_flow_through_middleware_normalization(deployment):
    """The middleware turns `memories/log.md` into the host-absolute path before
    the tool runs — that normalized path must work in the container."""
    from olano.security.deepagents_middleware import _normalize_path_tool_args

    backend, _home, ws = deployment
    args = _normalize_path_tool_args(
        "write_file", {"path": "memories/log.md"}, workspace_root=ws, mounts=[]
    )
    assert args["path"] == str(ws / "memories" / "log.md")

    assert backend.write(args["path"], "entry\n").error is None
    assert (ws / "memories" / "log.md").read_text(encoding="utf-8") == "entry\n"


def test_managed_skills_are_discovered_and_readable(deployment):
    """deepagents scans skill dirs THROUGH the backend and advertises the
    host-absolute SKILL.md path — both must work inside the container."""
    import asyncio

    skills_mod = pytest.importorskip("deepagents.middleware.skills")
    alist = getattr(skills_mod, "_alist_skills_with_errors", None)
    if alist is None:
        pytest.skip("deepagents renamed its skill-listing helper")
    backend, home, _ws = deployment

    found, error = asyncio.run(alist(backend, str(home / "skills")))
    assert error is None
    assert [s["name"] for s in found] == ["demo"]

    advertised = found[0]["path"]  # host-absolute, exactly as the prompt shows it
    read = backend.read(advertised)
    assert read.error is None
    assert "Use wisely" in read.file_data["content"]


def test_skill_root_is_read_only_in_the_container(deployment):
    backend, home, _ws = deployment

    result = backend.execute(f"touch {home}/skills/demo/hack.txt")
    assert result.exit_code != 0
    assert "read-only" in result.output.lower() or "permission denied" in result.output.lower()


def test_shared_folder_is_reachable_three_ways(deployment):
    backend, home, ws = deployment

    for path in (
        "/shared/default/handbook.md",  # container target
        f"{home}/shared/handbook.md",  # host path (prompt/tools style)
        f"{ws}/shared/default/handbook.md",  # workspace symlink projection
    ):
        read = backend.read(path)
        assert read.error is None, f"{path}: {read.error}"
        assert "common knowledge" in read.file_data["content"], path


def test_write_through_the_workspace_shared_projection_lands_in_the_source(deployment):
    backend, home, ws = deployment

    result = backend.execute(f"echo from-agent >> {ws}/shared/default/notes.md")
    assert result.exit_code == 0, result.output
    assert "from-agent" in (home / "shared" / "notes.md").read_text(encoding="utf-8")


def test_other_agents_workspaces_stay_invisible(deployment):
    """A normal agent sees ITS OWN workspace only — not its colleague's, and not
    the home root (vault, configs)."""
    backend, home, _ws = deployment

    other = backend.execute(f"ls {home}/agents/colleague/workspace")
    assert other.exit_code != 0

    assert backend.read(f"{home}/agents/colleague/workspace/PRIVATE.md").error is not None
    assert backend.execute(f"ls {home}/vault").exit_code != 0
    assert backend.execute(f"ls {home}/agents/merli/agent.json5").exit_code != 0


def test_stale_pre_namespace_container_is_recreated_not_reattached(deployment, tmp_path):
    """A persistent container made by pre-namespace code (different signature
    label) must be replaced on start(), or the new mounts silently never arrive."""
    import olano.core.builder as builder
    from olano.backends.lionclaw_docker import (
        OlanoDockerBackend,
        _persistent_container_name,
    )
    from olano.config.backend_config import OlanoDockerBackendConfig

    ws = tmp_path / "stale-ws"
    ws.mkdir()
    builder._ensure_sandbox_shared_dir(ws, recursive=True)
    name = _persistent_container_name("stale-agent", ws)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
    pre = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "olano.signature=deadbeefdeadbeef",
            SANDBOX_IMAGE,
            "sh",
            "-c",
            "sleep 3600",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert pre.returncode == 0, pre.stderr
    stale_id = pre.stdout.strip()

    backend = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="bridge", image=SANDBOX_IMAGE)
    )
    try:
        backend.start(agent_name="stale-agent", workspace_root=ws, shared_mounts=[])
        current = subprocess.run(
            ["docker", "inspect", name, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        assert current and not current.startswith(stale_id[:12]) and stale_id[:12] not in current
        # And the replacement really carries the identical-path mount:
        assert backend.execute(f"ls {ws.resolve()}").exit_code == 0
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
