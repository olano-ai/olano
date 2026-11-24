"""Live: the shell-family tools and ``sandboxed_run`` really run in the container.

Companion to ``test_sandbox_live.py`` (same prerequisites, same skip wording
so the sandbox-live workflow treats a missing Docker/image as a failure).
Where that module proves the backend primitives, this one proves the paths a
customer's agent actually takes to a shell on a managed box:

* the security middleware's chokepoint (``_run_shell_command``), which is what
  an agent turn reaches when the model calls ``safe_shell``;
* the tool bodies themselves (``safe_shell`` / ``node_run`` / ``npm_install``),
  which route through ``sandboxed_run`` and must never touch the host;
* ``sandboxed_run`` with a host ``cwd`` (one path namespace);
* and the two things an agent does first in a fresh sandbox: ``pip install``
  and ``npm install -g``, landing in the shared tool layer so the install
  outlives the container, then running what was installed.

Every test here runs under a **managed** posture, so a command that fell through
to the host would be refused rather than quietly pass: a green run proves the
container did the work. The install tests need egress to PyPI and the npm
registry, exactly like a customer's sandbox does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import olano.core.builder as builder
import olano.deployment.enforcement as enf
from olano.backends.lionclaw_docker import _TOOL_LAYER_DIR, OlanoDockerBackend
from olano.config.backend_config import OlanoDockerBackendConfig

SANDBOX_IMAGE = os.environ.get("LIONCLAW_SANDBOX_IMAGE", "olano-sandbox:latest")
AGENT = "pytest-tooling"


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
pytestmark = pytest.mark.skipif(not _READY, reason=f"live sandbox unavailable: {_WHY}")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A started sandbox over a seeded workspace and a runtime-shaped tool layer.

    ``ensure_tool_layer_layout`` is what the runtime does before mounting the
    layer: a bind mount shadows the image's ``/opt/agent-tools/*`` dirs, and
    npm 9 lstats ``<prefix>/lib`` on every ``npx``, so without it every global
    install dies ENOENT (see CLAUDE.md, interpreter shims).
    """
    root = tmp_path_factory.mktemp("live-tooling")
    ws, tools = root / "workspace", root / "tools"
    (ws / "proj").mkdir(parents=True)
    (ws / "proj" / "hello.js").write_text(
        "console.log('HELLO_FROM_NODE', process.cwd(), process.getuid())\n", encoding="utf-8"
    )
    builder._ensure_sandbox_shared_dir(ws, recursive=True)
    builder.ensure_tool_layer_layout(tools)

    backend = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="bridge", image=SANDBOX_IMAGE)
    )
    backend.start(
        agent_name=AGENT,
        workspace_root=ws,
        shared_mounts=[(str(tools), _TOOL_LAYER_DIR, "rw")],
    )
    yield backend, ws, tools
    subprocess.run(
        ["docker", "rm", "-f", backend.container_ref() or ""], capture_output=True, timeout=60
    )


@pytest.fixture
def managed_turn(live, monkeypatch):
    """A managed-posture turn of ``AGENT`` whose live sandbox is the module's.

    Managed is the point: on this posture the host lane is a refusal, so any
    output a test sees came from the container.
    """
    from olano.tools.talk_to import current_agent

    backend, ws, tools = live
    monkeypatch.setattr(enf, "feature_enabled", lambda name: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, AGENT, backend)
    token = current_agent.set(AGENT)
    try:
        yield backend, ws, tools
    finally:
        current_agent.reset(token)


def _no_host_processes(monkeypatch) -> None:
    """Fail loudly if anything other than the docker CLI is spawned on the host."""
    real_run = subprocess.run

    def guarded(args, *a, **kw):
        argv = args if isinstance(args, list | tuple) else [args]
        if not argv or Path(str(argv[0])).name != "docker":
            raise AssertionError(f"host process spawned by a shell tool: {argv!r}")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarded)


# ── the paths to a shell ─────────────────────────────────────────────────────


def test_middleware_chokepoint_runs_safe_shell_in_the_container(managed_turn, monkeypatch):
    """What an agent turn reaches: the middleware intercepts `safe_shell` by name
    and runs it through the live sandbox, never through the tool body or host."""
    import olano.security.deepagents_middleware as mw
    from olano.config.agent_config import AgentConfig

    backend, ws, _tools = managed_turn
    _no_host_processes(monkeypatch)
    msg = mw._run_shell_command(
        tool_name="safe_shell",
        args={"command": "id -u && ls /app /opt/olano 2>&1 | head -2", "timeout": 30},
        config=AgentConfig(name=AGENT, system_prompt="x"),
        workspace_root=ws,
        mounts=[],
        tool_call_id="live-mw-1",
        backend=backend,
    )
    assert msg.status == "success", msg.content
    assert "1000" in msg.content  # uid 1000: the container, not the root runtime
    assert "No such file" in msg.content or "cannot access" in msg.content  # no product source


def test_safe_shell_body_runs_in_the_container(managed_turn, monkeypatch):
    from olano.tools.shell import safe_shell_tool

    _no_host_processes(monkeypatch)
    out = safe_shell_tool.invoke({"command": "python -c 'import os; print(os.getuid())'"})
    assert out.strip() == "1000", out

    out = safe_shell_tool.invoke({"command": "ls /app"})
    assert "No such file" in out or "cannot access" in out, out


def test_sandboxed_run_cwd_is_the_mounted_workspace_path(managed_turn, monkeypatch):
    """One path namespace: the workspace is mounted at its host path, so a host
    `cwd` is valid inside the container unchanged."""
    from olano.tools._sandbox_exec import sandboxed_run

    _backend, ws, _tools = managed_turn
    _no_host_processes(monkeypatch)
    result = sandboxed_run(["pwd"], timeout=20, cwd=str(ws / "proj"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(ws / "proj")


def test_node_run_body_runs_a_workspace_script_in_the_container(managed_turn, monkeypatch):
    from olano.tools.node import node_run_tool

    _backend, ws, _tools = managed_turn
    _no_host_processes(monkeypatch)
    script = ws / "proj" / "hello.js"
    out = node_run_tool.func(str(script), cwd=str(ws / "proj"), timeout=60)
    assert "HELLO_FROM_NODE" in out, out
    assert str(ws / "proj") in out  # cwd honoured inside the container
    assert " 1000" in out  # getuid(): ran as the sandbox user


def test_managed_box_with_no_live_sandbox_refuses_instead_of_using_the_host(
    managed_turn, monkeypatch
):
    """Same posture, sandbox gone: the bodies refuse. This is the hole #238
    described, and a live posture check that the refusal is what a customer
    would see rather than a host process in the runtime container."""
    from olano.tools._sandbox_exec import sandboxed_run
    from olano.tools.node import npm_install_tool
    from olano.tools.shell import safe_shell_tool

    monkeypatch.delitem(builder._SANDBOX_BACKENDS, AGENT)

    def boom(*a, **k):
        raise AssertionError(f"host process spawned: {a!r}")

    monkeypatch.setattr(subprocess, "run", boom)

    assert "isolated sandbox" in safe_shell_tool.invoke({"command": "ls /"})
    assert "isolated sandbox" in npm_install_tool.func("cowsay", global_install=True)
    refused = sandboxed_run(["ffmpeg", "-version"], timeout=5)
    assert refused.returncode == 126 and "isolated sandbox" in refused.stderr


# ── installing tools, the first thing an agent does in a fresh sandbox ───────


def test_pip_install_lands_in_the_shared_tool_layer_and_imports(managed_turn, monkeypatch):
    """`PIP_USER=1` + `PYTHONUSERBASE` (injected on every exec) send a bare
    `pip install` into the shared layer, so the package outlives the container
    and `python` finds it on the next command."""
    from olano.tools.shell import safe_shell_tool

    _backend, _ws, tools = managed_turn
    _no_host_processes(monkeypatch)

    # `wcwidth`: pure Python, no dependencies, no console script (a script in
    # python/bin would shadow the npm CLI the next test installs, because
    # python/bin leads the tool-layer PATH). `--ignore-installed` makes the
    # probe independent of what the image ships: `six`, the first probe, is
    # already system-wide in the real image, so a plain install said "already
    # satisfied", put nothing in the layer and proved nothing.
    out = safe_shell_tool.invoke(
        {"command": "pip install --ignore-installed wcwidth==0.2.13", "timeout": 120}
    )
    assert "Successfully installed" in out, out

    # The module resolves from the shared layer, not from the image.
    out = safe_shell_tool.invoke({"command": "python -c 'import wcwidth; print(wcwidth.__file__)'"})
    assert out.strip().startswith("/opt/agent-tools/python/"), out

    # Host-visible: the install is in the bind-mounted layer, not the container.
    assert list(tools.glob("python/lib/python3*/site-packages/wcwidth/__init__.py")), sorted(
        str(p.relative_to(tools)) for p in tools.rglob("wcwidth*")
    )


def test_npm_global_install_lands_in_the_shared_tool_layer_and_runs(managed_turn, monkeypatch):
    """`npm_config_prefix` (injected on every exec) sends `npm install -g` into
    the shared layer whose `bin/` leads PATH, so the CLI runs by bare name
    afterwards, from `node_run` and from a plain shell alike."""
    from olano.tools.node import node_run_tool, npm_install_tool

    backend, _ws, tools = managed_turn
    _no_host_processes(monkeypatch)

    out = npm_install_tool.func("cowsay@1.6.0", global_install=True)
    assert "added" in out or "changed" in out or "up to date" in out, out
    assert (tools / "npm" / "bin" / "cowsay").exists(), sorted(
        str(p.relative_to(tools)) for p in (tools / "npm").rglob("cowsay*")
    )

    out = node_run_tool.func("cowsay LIVE_MOO", timeout=120)
    assert "LIVE_MOO" in out, out

    # And by bare name from a shell, through the tool-layer PATH the runtime exports.
    res = backend.execute("cowsay PATH_MOO")
    assert res.exit_code == 0 and "PATH_MOO" in res.output, res.output
