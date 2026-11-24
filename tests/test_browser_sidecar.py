"""The live browser's container lifecycle (faked docker CLI) and DevTools relay."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any

import pytest

from olano.browser import sidecar


class FakeDocker:
    """Answers the docker CLI calls sidecar makes; records every argv."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.images: set[str] = set()
        self.containers: dict[str, dict[str, Any]] = {}
        self.pull_fails = False
        self.build_output: str | None = None  # None: the build succeeds
        self.version_fails = False

    def __call__(
        self, cmd: list[str], timeout: float, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        ok = subprocess.CompletedProcess(cmd, 0, "", "")
        sub = cmd[1:3]
        if sub == ["image", "inspect"]:
            return (
                ok
                if cmd[3] in self.images
                else subprocess.CompletedProcess(cmd, 1, "", "no such image")
            )
        if cmd[1] == "pull":
            if self.pull_fails:
                return subprocess.CompletedProcess(
                    cmd, 1, "", "unauthorized: authentication required"
                )
            self.images.add(cmd[2])
            return ok
        if cmd[1] == "tag":
            self.images.add(cmd[3])
            return ok
        if cmd[1] == "build":
            if self.build_output is not None:
                return subprocess.CompletedProcess(cmd, 1, "", self.build_output)
            self.images.add(cmd[cmd.index("-t") + 1])
            return ok
        if cmd[1] == "inspect":
            info = self.containers.get(cmd[2])
            return (
                subprocess.CompletedProcess(cmd, 0, json.dumps([info]), "")
                if info
                else subprocess.CompletedProcess(cmd, 1, "[]", "")
            )
        if cmd[1] == "run":
            name = cmd[cmd.index("--name") + 1]
            sig = next(
                a.split("=", 2)[2]
                for a in cmd
                if a.startswith(f"--label={sidecar.LABEL_SIGNATURE}=")
            )
            self.containers[name] = {
                "State": {"Running": True},
                "Config": {"Labels": {sidecar.LABEL_SIGNATURE: sig, sidecar.LABEL: "1"}},
            }
            return ok
        if cmd[1] == "exec" and cmd[-1] == "version":
            if self.version_fails:
                return subprocess.CompletedProcess(cmd, 1, "", "devtools endpoint not ready")
            body = {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body) + "\n", "")
        if cmd[1:3] == ["rm", "-f"]:
            self.containers.pop(cmd[3], None)
            return ok
        if cmd[1:3] == ["ps", "-a"]:
            rows = "\n".join(f"{n}\tUp" for n in self.containers)
            return subprocess.CompletedProcess(cmd, 0, rows, "")
        return ok


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(sidecar, "_run", fake)
    monkeypatch.setattr(sidecar, "docker_available", lambda: True)
    monkeypatch.setattr(sidecar, "_network", lambda: "bridge")
    monkeypatch.delenv("OLANO_BROWSER_IMAGE", raising=False)
    monkeypatch.delenv("LIONCLAW_BROWSER_IMAGE", raising=False)
    sidecar._present_memo.clear()
    sidecar._pull_state.update(status="idle", error="")
    yield fake
    sidecar._present_memo.clear()


def _wait_pull() -> dict[str, Any]:
    import time

    deadline = time.time() + 5
    while time.time() < deadline and sidecar._pull_state["status"] in ("pulling", "building"):
        time.sleep(0.02)
    return dict(sidecar._pull_state)


def test_pull_fetches_the_source_and_tags_the_local_name(docker):
    sidecar.start_pull()
    state = _wait_pull()
    assert state["status"] == "ready", state
    assert ["docker", "pull", sidecar.DEFAULT_SOURCE_IMAGE] in docker.calls
    assert ["docker", "tag", sidecar.DEFAULT_SOURCE_IMAGE, "olano-browser:latest"] in docker.calls
    assert sidecar.image_present()


def test_a_refused_pull_builds_the_image_here(docker):
    """The published image is private and the runtime holds no registry login,
    so a refused pull must not leave a box without a live browser."""
    docker.pull_fails = True
    sidecar.start_pull()
    state = _wait_pull()
    assert state["status"] == "ready", state
    assert state["via"] == "build" and "unauthorized" in state["reason"]
    build = next(c for c in docker.calls if c[1] == "build")
    assert build[2:4] == ["-t", "olano-browser:latest"]
    assert build[4] == str(sidecar.build_context())
    assert sidecar.image_present()


def test_the_build_context_ships_with_the_runtime():
    context = sidecar.build_context()
    assert context is not None and (context / "Dockerfile").is_file()


def test_without_build_files_a_refused_pull_says_so(docker, monkeypatch, tmp_path):
    docker.pull_fails = True
    monkeypatch.setenv("LIONCLAW_BROWSER_BUILD_CONTEXT", str(tmp_path / "missing"))
    sidecar.start_pull()
    state = _wait_pull()
    assert state["status"] == "failed"
    assert "unauthorized" in state["error"] and "build files" in state["error"]
    assert not any(c[1] == "build" for c in docker.calls)


def test_a_failed_build_names_both_causes_and_the_package_error(docker):
    docker.pull_fails = True
    docker.build_output = (
        "#5 1.2 E: Unable to locate package chromium\n"
        'ERROR: failed to solve: process "/bin/sh -c apt-get update && apt-get install '
        'chromium" did not complete successfully: exit code: 100\n'
    )
    sidecar.start_pull()
    state = _wait_pull()
    assert state["status"] == "failed"
    assert "unauthorized" in state["error"]
    assert "E: Unable to locate package chromium" in state["error"]
    assert "/bin/sh -c" not in state["error"]


def test_present_image_is_not_pulled_again(docker):
    docker.images.add("olano-browser:latest")
    sidecar.start_pull()
    assert not any(c[1] == "pull" for c in docker.calls)


def test_container_missing_image_triggers_the_pull_and_says_so(docker):
    with pytest.raises(sidecar.SidecarUnavailable, match="being installed"):
        sidecar.ensure_container("merli")
    _wait_pull()
    assert any(c[1] == "pull" for c in docker.calls)


def test_container_is_hardened_and_holds_nothing(docker):
    docker.images.add("olano-browser:latest")
    name, version = sidecar.ensure_container("merli")
    assert name == "olano-browser-merli"
    assert version["webSocketDebuggerUrl"].startswith("ws://127.0.0.1:9222/")
    run = next(c for c in docker.calls if c[1] == "run")
    for flag in (
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        "--init",
        "--add-host=metadata.google.internal:127.0.0.1",
        f"--label={sidecar.LABEL}=1",
    ):
        assert flag in run
    assert any(a.startswith("--memory=") for a in run)
    # No published ports, no mounts: DevTools is reached only by docker exec.
    assert "-p" not in run and "--publish" not in run
    assert "-v" not in run and "--volume" not in run
    assert run[-1] == "olano-browser:latest"


def test_running_container_with_the_same_signature_is_reused(docker):
    docker.images.add("olano-browser:latest")
    sidecar.ensure_container("merli")
    sidecar.ensure_container("merli")
    assert sum(1 for c in docker.calls if c[1] == "run") == 1


def test_signature_change_recreates_the_container(docker, monkeypatch):
    docker.images.add("olano-browser:latest")
    sidecar.ensure_container("merli")
    monkeypatch.setenv("LIONCLAW_BROWSER_MEMORY", "2g")
    sidecar.ensure_container("merli")
    assert ["docker", "rm", "-f", "olano-browser-merli"] in docker.calls
    assert sum(1 for c in docker.calls if c[1] == "run") == 2


def test_a_proxy_is_part_of_the_container_recipe(docker):
    """The WebRTC-on-proxy flag is a launch flag of the browser inside, so the
    container carries it only for a proxied agent, and a proxy coming or
    going recreates the container."""
    docker.images.add("olano-browser:latest")
    sidecar.ensure_container("merli")
    first = next(c for c in docker.calls if c[1] == "run")
    assert "OLANO_BROWSER_PROXIED=1" not in first
    sidecar.ensure_container("merli", proxied=True)
    runs = [c for c in docker.calls if c[1] == "run"]
    assert len(runs) == 2 and "OLANO_BROWSER_PROXIED=1" in runs[1]
    assert ["docker", "rm", "-f", "olano-browser-merli"] in docker.calls
    sidecar.ensure_container("merli", proxied=True)
    assert sum(1 for c in docker.calls if c[1] == "run") == 2


def test_unready_container_is_removed(docker):
    docker.images.add("olano-browser:latest")
    docker.version_fails = True
    with pytest.raises(sidecar.SidecarUnavailable, match="never answered"):
        sidecar.ensure_container("merli")
    assert "olano-browser-merli" not in docker.containers


def test_reap_orphans_keeps_only_named_containers(docker):
    docker.images.add("olano-browser:latest")
    sidecar.ensure_container("a")
    sidecar.ensure_container("b")
    removed = sidecar.reap_orphans({"olano-browser-a"})
    assert removed == ["olano-browser-b"]
    assert list(docker.containers) == ["olano-browser-a"]


def test_agent_slug_keeps_names_apart():
    assert sidecar.agent_slug("merli") == "merli"
    a, b = sidecar.agent_slug("Team A"), sidecar.agent_slug("team-a")
    assert a != b
    assert sidecar.container_name("x" * 100).startswith(sidecar.CONTAINER_PREFIX)
    assert len(sidecar.container_name("x" * 100)) < 64


# ── relay ────────────────────────────────────────────────────────────────────


ECHO = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]


async def _exchange(port: int, head: bytes, body: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head + body)
    await writer.drain()
    writer.write_eof()
    data = await asyncio.wait_for(reader.read(), timeout=10)
    writer.close()
    return data


async def test_relay_refuses_a_connection_without_the_token():
    relay = sidecar.ExecPipeRelay(ECHO)
    port = await relay.start()
    try:
        out = await _exchange(port, b"GET /json/version HTTP/1.1\r\nHost: x\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403")
        wrong = await _exchange(port, b"GET / HTTP/1.1\r\nX-Olano-Relay: nope\r\n\r\n")
        assert wrong.startswith(b"HTTP/1.1 403")
    finally:
        await relay.close()


async def test_relay_forwards_the_whole_stream_once_authorized():
    relay = sidecar.ExecPipeRelay(ECHO)
    port = await relay.start()
    try:
        head = f"GET /devtools HTTP/1.1\r\nX-Olano-Relay: {relay.token}\r\n\r\n".encode()
        out = await _exchange(port, head, b"frame-1|frame-2")
        assert out == head + b"frame-1|frame-2"
    finally:
        await relay.close()


def test_relay_rewrites_the_devtools_url_onto_itself():
    relay = sidecar.ExecPipeRelay(ECHO)
    relay.port = 41234
    assert relay.ws_url("ws://127.0.0.1:9222/devtools/browser/abc") == (
        "ws://127.0.0.1:41234/devtools/browser/abc"
    )
