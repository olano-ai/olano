"""Live sandbox integration tests — a real Docker container, no mocks.

**Opt-in.** Skipped unless a Docker daemon is reachable AND the sandbox image is
present. Point at a different image with ``LIONCLAW_SANDBOX_IMAGE``; the default
is the same ``olano-sandbox:latest`` the runtime spawns::

    docker pull ghcr.io/olano-ai/olano-sandbox:latest \\
      && docker tag ghcr.io/olano-ai/olano-sandbox:latest olano-sandbox:latest
    uv run pytest tests/integration/test_sandbox_live.py -q

Why this exists: the unit suite fakes the `docker exec` hop, which is enough to
pin the deepagents protocol contract but blind to everything the kernel decides.
Three bugs got through mocked tests and only showed up here — a login shell
printing its profile into the JSON deepagents parses, a workspace the runtime
seeded as root that the uid-1000 agent could not write, and a 0600 run log the
agent was told to read but could not open. Anything touching the container
boundary (ownership, permissions, stdin, exit codes) belongs in this file.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

import pytest

import olano.core.builder as builder
from olano.backends.lionclaw_docker import _TOOL_LAYER_DIR, OlanoDockerBackend
from olano.config.backend_config import OlanoDockerBackendConfig

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
def live(tmp_path_factory):
    """A started sandbox over a workspace seeded as root, like the runtime does."""
    root = tmp_path_factory.mktemp("live-sandbox")
    ws, tools = root / "workspace", root / "tools"

    (ws / "memories").mkdir(parents=True)
    (ws / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
    (ws / "memories" / "log.md").write_text("today\n", encoding="utf-8")
    builder._ensure_sandbox_shared_dir(ws, recursive=True)
    builder._ensure_sandbox_shared_dir(tools)

    backend = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="bridge", image=SANDBOX_IMAGE)
    )
    backend.start(
        agent_name="pytest-live",
        workspace_root=ws,
        shared_mounts=[(str(tools), _TOOL_LAYER_DIR, "rw")],
    )
    yield backend, ws, tools
    subprocess.run(
        ["docker", "rm", "-f", backend.container_ref() or ""], capture_output=True, timeout=60
    )


def test_container_runs_with_the_hardening_flags(live):
    backend, _ws, _tools = live
    fmt = "{{.State.Status}}|{{.HostConfig.Memory}}|{{json .HostConfig.CapDrop}}|{{.HostConfig.PidsLimit}}"
    out = subprocess.run(
        ["docker", "inspect", backend.container_ref(), "--format", fmt],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    status, mem, caps, pids = out.split("|")
    assert status == "running"
    assert mem == str(512 * 1024 * 1024)
    assert "ALL" in caps
    assert pids == "512"


def test_commands_run_as_the_non_root_user_in_the_workspace(live):
    backend, _ws, _tools = live
    result = backend.execute("id -u; pwd")
    assert result.exit_code == 0
    assert "1000" in result.output
    assert "/workspace" in result.output


def test_sandbox_holds_no_product_source(live):
    backend, _ws, _tools = live
    out = backend.execute("ls /app; ls /opt/olano").output
    assert "No such file" in out or "cannot access" in out


def test_write_read_edit_roundtrip_crosses_the_bind_mount(live):
    backend, ws, _tools = live
    target = "/workspace/notes/live.txt"

    assert backend.write(target, "alpha\nbeta\n").error is None
    host = ws / "notes" / "live.txt"
    assert host.read_text(encoding="utf-8") == "alpha\nbeta\n"
    # Files the agent creates must stay the agent's, or its next edit fails.
    assert host.stat().st_uid == builder.SANDBOX_UID

    assert backend.read(target).file_data["content"] == "alpha\nbeta"
    assert backend.edit(target, "beta", "BETA").occurrences == 1
    assert "BETA" in host.read_text(encoding="utf-8")


def test_als_scans_a_real_directory(live):
    """The call from SkillsMiddleware.abefore_agent that broke every chat turn."""
    backend, ws, _tools = live
    (ws / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (ws / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: live\n---\n", encoding="utf-8"
    )

    result = asyncio.run(backend.als("/workspace/skills"))

    assert result.error is None
    assert any("demo" in e["path"] for e in result.entries)


def test_deepagents_skill_discovery_end_to_end(live):
    skills = pytest.importorskip("deepagents.middleware.skills")
    alist = getattr(skills, "_alist_skills_with_errors", None)
    if alist is None:
        pytest.skip("deepagents renamed its skill-listing helper")
    backend, ws, _tools = live
    (ws / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (ws / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: live\n---\n", encoding="utf-8"
    )

    found, error = asyncio.run(alist(backend, "/workspace/skills"))

    assert error is None
    assert [s["name"] for s in found] == ["demo"]


def test_deepagents_registers_the_execute_tool(live):
    from deepagents.middleware.filesystem import FilesystemMiddleware, supports_execution

    backend, _ws, _tools = live
    assert supports_execution(backend) is True
    assert "execute" in {t.name for t in FilesystemMiddleware(backend=backend).tools}


def test_binary_upload_download_roundtrip(live):
    backend, ws, _tools = live
    payload = bytes(range(256)) * 40

    (up,) = backend.upload_files([("/workspace/bin/blob.dat", payload)])
    assert up.error is None
    assert (ws / "bin" / "blob.dat").read_bytes() == payload
    assert (ws / "bin" / "blob.dat").stat().st_uid == builder.SANDBOX_UID

    (down,) = backend.download_files(["/workspace/bin/blob.dat"])
    assert down.error is None
    assert down.content == payload


def test_download_maps_kernel_errors_to_protocol_codes(live):
    backend, _ws, _tools = live
    missing, is_dir = backend.download_files(["/workspace/ghost", "/workspace/memories"])
    assert missing.error == "file_not_found"
    assert is_dir.error == "is_directory"


def test_execute_and_run_disagree_only_about_stream_merging(live):
    backend, _ws, _tools = live
    merged = backend.execute("echo out; echo err >&2; exit 7")
    split = backend.run("echo out; echo err >&2; exit 7")

    assert "out" in merged.output and "err" in merged.output and merged.exit_code == 7
    assert split.stdout.strip() == "out"
    assert split.stderr.strip() == "err"
    assert split.returncode == 7


def test_shell_output_is_not_polluted_by_a_shell_profile(live):
    """A login shell's /etc/profile banner would prepend itself to the single JSON
    line deepagents' file-op templates print, breaking every read_file/glob."""
    backend, _ws, _tools = live
    assert backend.execute("echo SOLO").output == "SOLO\n"


def test_tool_layer_env_reaches_every_command(live):
    backend, _ws, tools = live
    out = backend.execute("printenv PYTHONUSERBASE; echo $PATH").output
    assert "/opt/agent-tools/python" in out
    assert "/opt/agent-tools/npm/bin" in out

    assert backend.execute("touch /opt/agent-tools/probe").exit_code == 0
    assert (tools / "probe").exists()  # shared layer really is host-backed


def test_bare_python_resolves(live):
    """Models reach for `python` by reflex. On a python3-only image every first
    attempt died with "command not found" before the retry, so the alias is
    baked in (python-is-python3)."""
    backend, _ws, _tools = live
    result = backend.execute("python -c 'import sys; print(sys.version_info.major)'")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "3"


def test_tar_can_extract_a_zstd_archive(live):
    """`tar --zstd` execs the separate `zstd` binary, which the image lacked —
    modern release tarballs (Ollama's among them) ship as .tar.zst, and the
    non-root agent cannot apt-get one at run time."""
    backend, _ws, _tools = live
    result = backend.execute(
        "cd /tmp && rm -rf zt zt.tar.zst && mkdir zt && echo payload-ok > zt/f.txt"
        " && tar --zstd -cf zt.tar.zst zt && rm -rf zt"
        " && tar --zstd -xf zt.tar.zst && cat zt/f.txt"
    )
    assert result.exit_code == 0, result.output
    assert "payload-ok" in result.output


def test_everyday_shell_commands_resolve(live):
    """`unzip` was missing from the image, and so were ps, free, file, less,
    xz, dig and ssh: bookworm-slim ships none of them and the non-root agent
    cannot apt-get one at run time. Asks the shell, so a package that installs
    under a different binary name fails here, not in a user's Terminal."""
    backend, _ws, _tools = live
    commands = (
        "zip unzip bzip2 xz 7z file less tree patch xxd dos2unix bc envsubst "
        "ps top free pkill watch killall lsof htop ip ss ping dig nslookup nc "
        "ssh scp rsync make"
    )
    result = backend.execute(
        f"for c in {commands}; do command -v $c >/dev/null || echo MISSING:$c; done; echo done"
    )
    assert result.exit_code == 0, result.output
    assert "MISSING:" not in result.output, result.output


# One real operation per everyday tool, run as the sandbox user under the
# production flags (--cap-drop=ALL, no-new-privileges, uid 1000): a binary that
# is on PATH but cannot do its job here (ping without CAP_NET_RAW, htop or
# watch without a terminal, lsof without CAP_SYS_PTRACE) fails its own case
# instead of reading as installed. Each script prints its expected token only
# when the operation worked, in a fresh /tmp directory of its own.
# Runs argv in a 120x40 pty for two seconds, prints what it drew, kills it.
# Not `script`: given a /dev/null stdin it outlives its killed child forever.
_PTY = (
    'python3 -c "import fcntl, os, pty, select, struct, sys, termios, time\n'
    "pid, fd = pty.fork()\n"
    "if pid == 0:\n"
    "    fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))\n"
    "    os.environ['TERM'] = 'xterm'\n"
    "    os.execvp(sys.argv[1], sys.argv[1:])\n"
    "out, end = b'', time.time() + 2\n"
    "while time.time() < end:\n"
    "    if select.select([fd], [], [], 0.2)[0]:\n"
    "        try:\n"
    "            out += os.read(fd, 65536)\n"
    "        except OSError:\n"
    "            break\n"
    "os.kill(pid, 9)\n"
    'os.write(1, out)"'
)
# A background job keeps `docker exec` waiting on its stdout until it exits.
_BG = ">/dev/null 2>&1 </dev/null &"
EVERYDAY_SHELL_CASES = {
    "zip-unzip": (
        "mkdir d && echo zip-ok > d/f.txt && zip -qr a.zip d && unzip -q a.zip -d out"
        " && cat out/d/f.txt",
        "zip-ok",
    ),
    "unzip-list": (
        "mkdir d && echo x > d/f.txt && zip -qr a.zip d && unzip -l a.zip | grep -q d/f.txt"
        " && echo list-ok",
        "list-ok",
    ),
    "7z": (
        "mkdir d && echo 7z-ok > d/f.txt && 7z a -bd a.7z d >/dev/null && rm -r d"
        " && 7z x -bd a.7z >/dev/null && cat d/f.txt",
        "7z-ok",
    ),
    "tar-xz": (
        "mkdir d && echo xz-ok > d/f.txt && tar -cJf a.tar.xz d && rm -r d"
        " && tar -xJf a.tar.xz && cat d/f.txt",
        "xz-ok",
    ),
    "tar-bz2": (
        "mkdir d && echo bz2-ok > d/f.txt && tar -cjf a.tar.bz2 d && rm -r d"
        " && tar -xjf a.tar.bz2 && cat d/f.txt",
        "bz2-ok",
    ),
    "file": (
        "echo hi > t.txt && zip -q a.zip t.txt && file t.txt a.zip",
        "Zip archive",
    ),
    "less": ("printf 'less-ok\\n' > t && less -FX t", "less-ok"),
    "tree": ("mkdir -p d/sub && touch d/sub/leaf && tree d", "leaf"),
    "patch": (
        "printf 'a\\nb\\n' > f && printf 'a\\npatched-ok\\n' > g && (diff -u f g > p; true)"
        " && patch -s f < p && cat f",
        "patched-ok",
    ),
    "xxd": ("printf AB | xxd -p && printf 4142 | xxd -r -p && echo", "AB"),
    "dos2unix": (
        "printf 'a\\r\\n' > f && dos2unix -q f && [ \"$(od -An -c f | tr -d ' \\n')\" = 'a\\n' ]"
        " && echo crlf-gone",
        "crlf-gone",
    ),
    "bc": ("echo '2^10' | bc", "1024"),
    "envsubst": ("echo 'v=$V' | V=subst-ok envsubst", "v=subst-ok"),
    # Copies of sleep under unique names, matched by exact process name, so
    # nothing can match the shell running the case (its argv names them too).
    "ps": (
        f"cp /bin/sleep zps && ./zps 30 {_BG} sleep 0.2; ps -eo comm | grep -qx zps"
        " && echo ps-ok; kill $!",
        "ps-ok",
    ),
    "top": ("top -bn1 | head -1", "load average"),
    "free": ("free -m", "Mem:"),
    "pkill-pgrep": (
        f"cp /bin/sleep zpk && ./zpk 30 {_BG} sleep 0.2; pgrep -x zpk >/dev/null"
        " && pkill -x zpk && sleep 0.3 && ! pgrep -x zpk && echo pkill-ok",
        "pkill-ok",
    ),
    "killall": (
        f"cp /bin/sleep zka && ./zka 30 {_BG} sleep 0.2; killall zka"
        " && sleep 0.3 && ! pgrep -x zka && echo killall-ok",
        "killall-ok",
    ),
    "lsof": ("exec 9>held.txt && lsof -p $$ | grep held.txt && exec 9>&-", "held.txt"),
    # watch and htop are full-screen programs: they get a real pty, as in the
    # dashboard Terminal, and are killed after two seconds of drawing.
    "watch": (f"{_PTY} watch -n 0.5 echo watch-ok", "watch-ok"),
    "htop": (f"{_PTY} htop", "Load average"),
    "ip": ("ip -brief addr && ip route", "default via"),
    "ss": ("ss -tan", "State"),
    "ping": (
        "ping -c1 -W3 $(ip route | awk '/^default/ {print $3}') >/dev/null && echo ping-ok",
        "ping-ok",
    ),
    "nc": (
        "nc -l 127.0.0.1 39123 > got 2>/dev/null </dev/null & sleep 0.5 && echo nc-ok | nc -N 127.0.0.1 39123"
        " ; sleep 0.3 ; cat got",
        "nc-ok",
    ),
    "ssh": (
        "ssh-keygen -q -t ed25519 -N '' -f k && ssh -G example.com | grep -q '^hostname '"
        " && cat k.pub | cut -d' ' -f1",
        "ssh-ed25519",
    ),
    "scp-rsync": (
        "mkdir s && echo copy-ok > s/f && scp -q s/f c && rsync -a s/ r/ && cat c r/f",
        "copy-ok\ncopy-ok",
    ),
    "make": (
        "printf 'all:\\n\\t@echo make-ok\\n' > Makefile && make",
        "make-ok",
    ),
}


@pytest.mark.parametrize("case", sorted(EVERYDAY_SHELL_CASES))
def test_everyday_shell_tool_works(live, case):
    """`unzip` in a dashboard Terminal said "command not found"; this proves the
    replacement does the job, not just that it resolves."""
    backend, _ws, _tools = live
    script, expected = EVERYDAY_SHELL_CASES[case]
    result = backend.execute(f'cd "$(mktemp -d)" && {{ {script} ; }} 2>&1')
    assert expected in result.output, f"{case}: exit {result.exit_code}\n{result.output}"


def test_timeout_comes_back_as_exit_124_not_an_exception(live):
    backend, _ws, _tools = live
    result = backend.execute("sleep 10", timeout=2)
    assert result.exit_code == 124
    assert "timed out" in result.output.lower()


def test_agent_can_write_the_workspace_the_runtime_seeded(live):
    """Chowning only the top dir left MEMORY.md and memories/ unwritable."""
    backend, _ws, _tools = live
    assert backend.execute("echo more >> /workspace/MEMORY.md").exit_code == 0
    assert backend.execute("touch /workspace/memories/new.md").exit_code == 0
    assert backend.edit("/workspace/MEMORY.md", "# memory", "# memory v2").error is None
    assert backend.write("/workspace/memories/log.md", "rewritten\n").error is None


def test_run_log_written_by_root_at_0600_is_readable_by_the_agent(live):
    """What `/api/shell/run` does after a sandbox run — the agent is handed this
    path and told to read it, so it has to be able to open it."""
    backend, ws, _tools = live
    logs = ws / ".olano" / "shell_runs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "live-run.log"
    log.write_text("FULL_OUTPUT\n", encoding="utf-8")
    log.chmod(0o600)

    before = backend.read("/workspace/.olano/shell_runs/live-run.log")
    builder.chown_for_sandbox(logs)
    builder.chown_for_sandbox(log)
    after = backend.read("/workspace/.olano/shell_runs/live-run.log")

    if os.geteuid() == 0:  # only root can demonstrate the "before" failure
        assert before.error is not None
    assert after.error is None
    assert "FULL_OUTPUT" in after.file_data["content"]


def test_lionclaw_shell_middleware_routes_into_the_container(live):
    import olano.security.deepagents_middleware as mw
    from olano.config.agent_config import AgentConfig

    backend, ws, _tools = live
    msg = mw._run_shell_command(
        tool_name="execute",
        args={"command": "echo FROM_SANDBOX && id -u", "timeout": 20},
        config=AgentConfig(name="pytest-live", system_prompt="x"),
        workspace_root=ws,
        mounts=[],
        tool_call_id="live-1",
        backend=backend,
    )
    assert msg.status == "success"
    assert "FROM_SANDBOX" in msg.content
    assert "1000" in msg.content  # really the container, not the root runtime


def test_sandboxed_run_sends_in_process_clis_into_the_container(live, monkeypatch):
    from olano.tools._sandbox_exec import sandbox_active_for_current_agent, sandboxed_run
    from olano.tools.talk_to import current_agent

    backend, _ws, _tools = live
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, "pytest-live", backend)
    token = current_agent.set("pytest-live")
    try:
        assert sandbox_active_for_current_agent() is True
        result = sandboxed_run(["python3", "-c", "print('CLI_IN_SANDBOX')"], timeout=30)
    finally:
        current_agent.reset(token)

    assert result.returncode == 0
    assert "CLI_IN_SANDBOX" in result.stdout


def test_a_fresh_backend_reattaches_to_the_same_container(live):
    backend, ws, tools = live
    twin = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="bridge", image=SANDBOX_IMAGE)
    )
    twin.start(
        agent_name="pytest-live",
        workspace_root=ws,
        shared_mounts=[(str(tools), _TOOL_LAYER_DIR, "rw")],
    )

    assert twin.container_ref() == backend.container_ref()
    assert twin.execute("echo REATTACHED").output.strip() == "REATTACHED"


def test_configured_limits_reach_the_container(tmp_path):
    """The caps used to be hardcoded at 512m/0.5 whatever the machine was. What
    the builder resolves has to survive the whole way to `docker run` — the unit
    suite only sees the argv, not what the daemon did with it."""
    backend = OlanoDockerBackend(
        OlanoDockerBackendConfig(
            persistent=True,
            network="bridge",
            image=SANDBOX_IMAGE,
            memory="768m",
            cpus=1.0,
        )
    )
    ws = tmp_path / "limits-ws"
    ws.mkdir()
    builder._ensure_sandbox_shared_dir(ws, recursive=True)
    try:
        backend.start(agent_name="pytest-limits", workspace_root=ws, shared_mounts=[])
        out = subprocess.run(
            [
                "docker",
                "inspect",
                backend.container_ref(),
                "--format",
                "{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        mem, nano_cpus = out.split("|")
        assert mem == str(768 * 1024 * 1024)
        assert nano_cpus == str(10**9)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", backend.container_ref() or ""], capture_output=True, timeout=60
        )


def test_changed_limits_recreate_the_persistent_container(tmp_path):
    """Docker cannot retune a live container's caps, so reattaching to one built
    with the old limits would silently keep them. The signature carries the
    limits precisely so a raised ceiling actually takes effect."""
    ws = tmp_path / "recreate-ws"
    ws.mkdir()
    builder._ensure_sandbox_shared_dir(ws, recursive=True)

    def _backend(memory: str) -> OlanoDockerBackend:
        return OlanoDockerBackend(
            OlanoDockerBackendConfig(
                persistent=True, network="bridge", image=SANDBOX_IMAGE, memory=memory, cpus=0.5
            )
        )

    # A persistent container keeps its name across a rebuild (that is how the
    # next start finds it), so identity is the container id, not container_ref().
    def _inspect(ref: str | None) -> tuple[str, str]:
        out = subprocess.run(
            ["docker", "inspect", ref or "", "--format", "{{.Id}}|{{.HostConfig.Memory}}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        cid, mem = out.split("|")
        return cid, mem

    first = _backend("512m")
    second = _backend("768m")
    try:
        first.start(agent_name="pytest-recreate", workspace_root=ws, shared_mounts=[])
        original, mem = _inspect(first.container_ref())
        assert mem == str(512 * 1024 * 1024)

        twin = _backend("512m")
        twin.start(agent_name="pytest-recreate", workspace_root=ws, shared_mounts=[])
        assert _inspect(twin.container_ref())[0] == original, "same limits must reattach, not churn"

        second.start(agent_name="pytest-recreate", workspace_root=ws, shared_mounts=[])
        recreated, mem = _inspect(second.container_ref())
        assert recreated != original
        assert mem == str(768 * 1024 * 1024)
        assert second.execute("echo RECREATED").output.strip() == "RECREATED"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", second.container_ref() or first.container_ref() or ""],
            capture_output=True,
            timeout=60,
        )


def test_terminal_resolves_the_same_container(live, monkeypatch):
    from olano.runtime import terminal as terminal_mod

    backend, _ws, _tools = live
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, "pytest-live", backend)

    ref = terminal_mod.sandbox_container_ref("pytest-live")

    assert ref == backend.container_ref()
    probe = subprocess.run(
        ["docker", "exec", ref, "sh", "-lc", "echo PTY_TARGET_OK"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "PTY_TARGET_OK" in probe.stdout


def test_chat_bang_lane_runs_in_the_sandbox(live, monkeypatch, tmp_path):
    """`POST /api/shell/run` under a managed posture, against the live sandbox."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    import olano.runtime.api as api_mod
    from olano.orchestration.event_bus import EventBus
    from olano.runtime import terminal as terminal_mod
    from olano.webhooks.db import WebhookDB
    from olano.webhooks.dispatcher import WebhookDispatcher

    backend, ws, _tools = live
    monkeypatch.setattr(terminal_mod, "terminal_enabled", lambda: True)
    monkeypatch.setattr(terminal_mod, "terminal_uses_sandbox", lambda: True)
    monkeypatch.setattr(terminal_mod, "sandbox_backend_for", lambda agent: backend)
    monkeypatch.setattr(api_mod, "_workspace_root", lambda agent=None: ws)

    runtime = MagicMock()
    runtime._runners, runtime._agent_resources, runtime._blocked_agents = {}, {}, {}
    runtime.registry.get.return_value = None
    runtime.webhook_dispatcher = WebhookDispatcher(
        EventBus(), db=WebhookDB(db_path=tmp_path / "wh.db")
    )
    client = TestClient(api_mod.create_api(runtime), raise_server_exceptions=False)

    resp = client.post("/api/shell/run", json={"command": "id -un && pwd", "agent": "pytest-live"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exit_code"] == 0
    assert "sandbox" in "\n".join(body["head"])  # ran as the container user
    assert "/workspace" in "\n".join(body["head"])

    # The agent is handed this path — it must be able to open it.
    read_back = backend.read(f"/workspace/{body['log_path']}")
    assert read_back.error is None
    assert "sandbox:" in read_back.file_data["content"]


def test_builder_starts_and_registers_an_explicit_docker_backend(tmp_path, monkeypatch):
    """The bug in the wild, against a real daemon.

    An `agent.json5` saying `backend: {type: "lionclaw_docker"}` used to yield an
    UNSTARTED backend: every file and shell tool answered exit 127 ("start() has
    not been called"), while the terminal and chat `!` lanes reported no sandbox
    at all because nothing was registered. Every other test in this file calls
    `backend.start()` by hand, so none of them could see it — this one goes
    through the real build path.
    """
    import olano.deployment.enforcement as enf
    from olano.backends.lionclaw_docker import _persistent_container_name
    from olano.runtime import terminal as terminal_mod

    ws, tools = tmp_path / "workspace", tmp_path / "tools"
    (ws / "memories").mkdir(parents=True)
    (ws / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
    builder._ensure_sandbox_shared_dir(ws, recursive=True)
    builder._ensure_sandbox_shared_dir(tools)

    # Managed + entitled: exercises the posture gate and the hardening coercions.
    monkeypatch.setattr(enf, "feature_enabled", lambda n: True)
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(enf, "sandbox_exec_enabled", lambda: True)
    # …so the image coercion resolves to the image this runner actually has.
    monkeypatch.setenv("LIONCLAW_SANDBOX_IMAGE", SANDBOX_IMAGE)

    agent = "pytest-live-explicit"
    try:
        backend = builder._build_deepagents_backend(
            # network="host" here proves the managed coercion against a real daemon.
            OlanoDockerBackendConfig(persistent=True, network="host", image=SANDBOX_IMAGE),
            ws,
            agent_name=agent,
            shared_mounts=[(str(tools), _TOOL_LAYER_DIR, "rw")],
        )

        assert isinstance(backend, OlanoDockerBackend)
        assert backend.container_ref(), "the build must leave a live container behind"

        # The container is real and non-root, and /workspace is the working dir.
        assert "1000" in backend.execute("id -u").output
        assert "/workspace" in backend.execute("pwd").output

        # Registered — this is what `olano_run_agent_command` and the Terminal read.
        assert builder.get_sandbox_backend(agent) is backend
        assert terminal_mod.sandbox_container_ref(agent) == backend.container_ref()
        assert builder.sandbox_status(agent)["state"] == builder.SANDBOX_STARTED

        # Host paths resolve inside the container — the one-path-namespace
        # contract that exit-127 used to hide.
        assert backend.read(str(ws / "MEMORY.md")).error is None

        # network=host must have been refused on a managed box.
        mode = subprocess.run(
            [
                "docker",
                "inspect",
                backend.container_ref(),
                "--format",
                "{{.HostConfig.NetworkMode}}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        # Coerced onto the default lane: the isolated sandbox network, or the
        # shared bridge where isolation is off (`LIONCLAW_SANDBOX_ISOLATION=0`).
        assert mode != "host"
        assert mode == backend._effective_network()
        assert mode in ("bridge", "olano-sandboxes")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", _persistent_container_name(agent, ws)],
            capture_output=True,
            timeout=60,
        )


def test_bare_python_resolves_without_the_image_alias(live):
    """The alias above is image-baked and still failed in production on a stale
    image/container. The runtime's shim in the shared tool layer must carry a
    bare `python` on its own: a PATH holding only `python3` plus the shim dir."""
    from olano.backends.interpreter_shims import ensure_interpreter_shims

    backend, _ws, tools = live
    # The host side writes the shims (builder._resolve_sandbox_shared_mounts in
    # the runtime); this fixture builds the backend directly, so do it here.
    ensure_interpreter_shims(tools)
    assert (tools / "shims" / "python").exists()
    result = backend.execute(
        'd=$(mktemp -d); ln -s "$(command -v python3)" "$d/python3"; '
        "PATH=\"$d:/opt/agent-tools/shims\" python -c 'print(7)'"
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "7"
    # And the image alias (or any earlier `python`) still wins over the shim.
    which = backend.execute("command -v python").output.strip()
    assert which != "/opt/agent-tools/shims/python", which


# ── the document toolchain ───────────────────────────────────────────────────
#
# The image bakes LibreOffice, tesseract, ImageMagick and qpdf so an agent can
# convert a document, read a scan and merge PDFs without installing anything.
# Every claim below is one an agent acts on from a SKILL.md, and every one of
# them is a property of the image and the kernel rather than of our code — which
# is exactly the class the unit suite cannot see.


def test_the_document_toolchain_answers(live):
    """The four binaries the document skills now promise. `--version` and not
    `command -v`: a package can install a wrapper whose real payload is
    missing, and an agent finds that out mid-task."""
    backend, _ws, _tools = live
    for probe, expect in (
        ("soffice --version", "LibreOffice"),
        ("tesseract --version", "tesseract"),
        ("qpdf --version", "qpdf version"),
        # bookworm ships ImageMagick 6, whose CLI is `convert` (`magick` is 7).
        ("convert -version", "ImageMagick"),
    ):
        result = backend.execute(probe)
        assert result.exit_code == 0, f"{probe}: {result.output}"
        assert expect.lower() in result.output.lower(), f"{probe}: {result.output}"


def test_tesseract_has_the_language_data_not_just_the_engine(live):
    """`--no-install-recommends` is on the apt layer and the language data are
    *Recommends*, so the engine installs with no data at all — and then fails
    at the first real page rather than at build time. `osd` matters separately:
    it is what reads a sideways scan's orientation."""
    backend, _ws, _tools = live
    result = backend.execute("tesseract --list-langs")
    assert result.exit_code == 0, result.output
    langs = {line.strip() for line in result.output.splitlines()}
    assert "eng" in langs, result.output
    assert "osd" in langs, result.output


def test_a_docx_converts_to_a_pdf(live):
    """The conversion the document skills' optional LibreOffice recipe runs
    (docx, pptx and xlsx in the catalog, olano-ai/skills). Also the one that
    exercises LibreOffice's user profile: the first run on a box builds it,
    which took long enough on a 0.5-CPU sandbox to hit the exec timeout, so the
    image pre-builds it as uid 1000 — a profile owned by root would be rebuilt
    here anyway and this would be slow, not failing, which is why the timeout
    is deliberately tight."""
    backend, _ws, _tools = live
    build = backend.execute(
        "cd /tmp && rm -rf docxlive && mkdir -p docxlive/word docxlive/_rels && cd docxlive && "
        "printf '%s' '<?xml version=\"1.0\"?><Types xmlns=\"http://schemas.openxmlformats.org/"
        'package/2006/content-types"><Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.'
        "main+xml\"/></Types>' > '[Content_Types].xml' && "
        "printf '%s' '<?xml version=\"1.0\"?><Relationships xmlns=\"http://schemas.openxmlformats."
        'org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/'
        "document.xml\"/></Relationships>' > _rels/.rels && "
        "printf '%s' '<?xml version=\"1.0\"?><w:document xmlns:w=\"http://schemas.openxmlformats."
        'org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Live sandbox paragraph</w:t>'
        "</w:r></w:p></w:body></w:document>' > word/document.xml && "
        "python3 -c \"import shutil, zipfile, os; z=zipfile.ZipFile('/tmp/live.docx','w'); "
        "[z.write(os.path.join(r,f), os.path.relpath(os.path.join(r,f),'.')) "
        "for r,_,fs in os.walk('.') for f in fs]; z.close()\" && ls -l /tmp/live.docx"
    )
    assert build.exit_code == 0, build.output

    convert = backend.execute(
        "cd /tmp && soffice --headless --convert-to pdf --outdir /tmp live.docx", timeout=120
    )
    assert convert.exit_code == 0, convert.output
    head = backend.execute("head -c 5 /tmp/live.pdf")
    assert head.output.strip().startswith("%PDF"), head.output
    # …and poppler can read back what LibreOffice wrote, which is the whole
    # docx → PDF → page-image route a visual check takes.
    text = backend.execute("pdftotext -layout /tmp/live.pdf - | head -5")
    assert "Live sandbox paragraph" in text.output, text.output


def test_fontconfig_sees_the_system_fonts_and_the_skill_faces(live):
    """Two independent halves. Liberation 2 is metric-compatible with Arial /
    Times New Roman / Courier New, so a converted document does not reflow —
    LibreOffice pulls in no fonts of its own, so without this every conversion
    substituted. And the `canvas-design` skill's bundled faces reach the
    container only through the shared tool layer: they are mounted at a host
    path that is unknowable at build time, so the image ships a fontconfig
    fragment pointing at the tool layer's fixed container path instead."""
    backend, _ws, tools = live
    fonts = tools / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    from olano.backends.interpreter_shims import ensure_skill_fonts

    ensure_skill_fonts(tools)
    if not any(fonts.glob("*.ttf")):
        pytest.skip("no bundled skill fonts on this checkout")
    builder._ensure_sandbox_shared_dir(fonts, recursive=True)

    system = backend.execute("fc-list | grep -ci liberation")
    assert system.exit_code == 0 and int(system.output.strip() or 0) > 0, system.output

    # fc-cache writes into the user's own cache, which is why this runs as the
    # agent rather than needing root.
    seen = backend.execute(
        "fc-cache -f >/dev/null 2>&1; fc-list : file | grep -c agent-tools/fonts"
    )
    assert int(seen.output.strip() or 0) > 0, seen.output


def test_imagemagick_refuses_pdf_by_policy_not_by_accident(live):
    """The distribution ships a policy that blocks PDF/PS/EPS (CVE-2016-3714).
    Pinned because rasterising goes through poppler instead, and a wrong
    reason there sends an agent hunting for a missing delegate. A future
    image that unblocks it would make that instruction wrong, loudly, here."""
    backend, _ws, _tools = live
    ok = backend.execute("cd /tmp && convert -size 8x8 xc:white pol.png && ls pol.png")
    assert ok.exit_code == 0, ok.output
    refused = backend.execute("cd /tmp && convert pol.png pol.pdf 2>&1; echo rc=$?")
    assert "rc=0" not in refused.output or "not authorized" in refused.output.lower(), (
        refused.output
    )


def test_a_file_the_runtime_renders_belongs_to_the_agent(live, monkeypatch):
    """The other half of this change: a chart the RUNTIME wrote into the
    workspace is root-owned across a bind mount, so the agent that asked for it
    could read it and not overwrite. `handoff_to_sandbox` hands it over — and
    only a real container can show the difference, because the failure is the
    kernel refusing a write, not a Python error.

    The backend is registered under the agent's name for the duration: this
    fixture builds one directly rather than through `build_agent`, and the
    helper resolves the sandbox the way the runtime does. Which name resolves to
    which backend is the half that IS unit-testable, and is pinned in
    `tests/test_sandbox_write_handoff.py`.
    """
    from olano.tools import _sandbox_exec

    backend, ws, _tools = live
    monkeypatch.setitem(builder._SANDBOX_BACKENDS, "pytest-live", backend)
    renders = ws / "reports"
    renders.mkdir(parents=True, exist_ok=True)

    stranded = renders / "root-owned.png"
    stranded.write_bytes(b"\x89PNG")
    keeper = renders / "handed-over.png"
    keeper.write_bytes(b"\x89PNG")
    builder.chown_for_sandbox(renders)
    _sandbox_exec.handoff_to_sandbox(keeper, agent="pytest-live")

    if os.geteuid() == 0:
        # Both live in a directory the agent owns, so the unlink itself is
        # permitted either way — what the hand-off buys is the WRITE.
        assert backend.execute("printf x >> /workspace/reports/root-owned.png").exit_code != 0
    assert backend.execute("printf x >> /workspace/reports/handed-over.png").exit_code == 0
    assert backend.execute("rm /workspace/reports/handed-over.png").exit_code == 0


def test_the_network_probe_reads_a_real_container_as_reachable(live):
    """The repair in `_start_persistent` rebuilds a container the probe calls
    `stranded`, so the probe's false-positive rate IS the cost of the feature —
    and only a real container exercises what it actually reads (`/proc/net/route`
    for the gateway, a connect, `/proc/net/arp` for a complete neighbour entry).
    A healthy sandbox must read `ok`.
    """
    from olano.backends.lionclaw_docker import probe_container_network

    backend, _ws, _tools = live
    state, detail = probe_container_network(backend.container_ref() or "")
    assert state == "ok", f"{state}: {detail}"
    assert detail  # the gateway it reached, e.g. 172.17.0.1


def test_a_deliberately_network_free_container_is_never_called_stranded(tmp_path_factory):
    """`network: none` means no network ON PURPOSE. It has no default route, so
    the probe must answer `unknown` — anything else would have the runtime
    rebuild such a sandbox on every single boot, forever.
    """
    from olano.backends.lionclaw_docker import probe_container_network

    root = tmp_path_factory.mktemp("live-sandbox-none")
    ws = root / "workspace"
    ws.mkdir(parents=True)
    builder._ensure_sandbox_shared_dir(ws, recursive=True)

    backend = OlanoDockerBackend(
        OlanoDockerBackendConfig(persistent=True, network="none", image=SANDBOX_IMAGE)
    )
    backend.start(agent_name="pytest-live-none", workspace_root=ws)
    try:
        assert backend._effective_network() == "none"  # never resolved away
        state, detail = probe_container_network(backend.container_ref() or "")
        assert state == "unknown", f"{state}: {detail}"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", backend.container_ref() or ""], capture_output=True, timeout=60
        )
