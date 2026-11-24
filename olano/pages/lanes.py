"""Where a page's commands run: the agent's sandbox, or the host on the open posture.

Both lanes offer the same two operations, so the supervisor never branches on
the posture:

* :meth:`Lane.run_build` runs a bounded build command in the page directory,
  streaming its output into the build log as it happens (the agent can watch
  it with its file tools), and returns the exit status.
* :meth:`Lane.spawn_keeper` starts the page's keeper (:mod:`olano.pages.keeper`)
  with piped stdin/stdout for the mux transport.

The sandbox lane goes through :meth:`OlanoDockerBackend.exec_argv`, the one
definition of "a command in the agent's sandbox", so a page command gets the
same environment, tool layer and egress policy as any other agent command. A
timeout is enforced *inside* the container with ``timeout -k``, because killing
the ``docker exec`` client does not stop what it started; on the host the build
runs in its own session and the whole process group is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from olano.pages.keeper import MARKER

logger = logging.getLogger(__name__)

#: Exit status GNU ``timeout`` reports when it had to stop the command.
TIMEOUT_EXIT = 124
_KILL_AFTER = 15
_PIPE_LIMIT = 1024 * 1024


@lru_cache(maxsize=1)
def keeper_source() -> str:
    """The keeper's source text, which is what runs in the sandbox."""
    from olano.pages import keeper

    return Path(keeper.__file__).read_text(encoding="utf-8")


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    code: int | None
    seconds: float
    timed_out: bool = False
    error: str = ""


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Lane(ABC):
    name: str = ""

    @property
    def isolated(self) -> bool:
        """Whether nothing else on the network can open a connection to a server here."""
        return False

    @abstractmethod
    def identity(self) -> str:
        """Changes when the execution target is replaced (a rebuilt sandbox)."""

    @abstractmethod
    async def run_build(
        self, command: str, *, cwd: Path, env: dict[str, str], timeout: int, log_path: Path
    ) -> BuildResult: ...

    @abstractmethod
    async def spawn_keeper(
        self, spec: dict[str, Any], *, env: dict[str, str]
    ) -> asyncio.subprocess.Process: ...


class SandboxLane(Lane):
    """Commands run in the agent's live sandbox container via ``docker exec``."""

    name = "sandbox"

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    @property
    def isolated(self) -> bool:
        try:
            return bool(self.backend.isolated)
        except Exception:
            return False

    def identity(self) -> str:
        return str(
            getattr(self.backend, "_container_id", None)
            or getattr(self.backend, "_container_name", None)
            or id(self.backend)
        )

    @staticmethod
    def _exports(env: dict[str, str]) -> str:
        return "".join(f"export {k}={shlex.quote(v)}; " for k, v in env.items())

    async def run_build(
        self, command: str, *, cwd: Path, env: dict[str, str], timeout: int, log_path: Path
    ) -> BuildResult:
        log = shlex.quote(str(log_path))
        header = f"[olano {_stamp()}] build: {command}\n"
        script = (
            f"mkdir -p {shlex.quote(str(log_path.parent))} && "
            f"printf '%s' {shlex.quote(header)} > {log} && "
            f"cd {shlex.quote(str(cwd))} && {self._exports(env)}"
            f"timeout -k {_KILL_AFTER} {int(timeout)} sh -c {shlex.quote(command)} >> {log} 2>&1"
        )
        try:
            argv = self.backend.exec_argv(script)
        except RuntimeError as exc:
            return BuildResult(False, None, 0.0, error=str(exc))
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            # The in-container `timeout` is the real bound; this one only
            # catches a docker CLI that hangs on its own.
            _, err = await asyncio.wait_for(proc.communicate(), timeout + _KILL_AFTER + 60)
        except asyncio.CancelledError:
            # Shutting down mid-build. Killing the client does not stop the
            # build inside the container; its own `timeout` still bounds it.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return BuildResult(False, None, time.monotonic() - started, True, "docker exec hung")
        seconds = time.monotonic() - started
        code = proc.returncode
        if code == TIMEOUT_EXIT or (code is not None and code in (128 + 9, 137)):
            return BuildResult(False, code, seconds, True, f"build exceeded {timeout}s")
        if code != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[-500:]
            return BuildResult(
                False, code, seconds, error=detail or f"build exited with code {code}"
            )
        return BuildResult(True, 0, seconds)

    async def spawn_keeper(
        self, spec: dict[str, Any], *, env: dict[str, str]
    ) -> asyncio.subprocess.Process:
        command = (
            f"{self._exports(env)}exec python3 -c {shlex.quote(keeper_source())} "
            f"{MARKER} {shlex.quote(json.dumps(spec, separators=(',', ':')))}"
        )
        argv = self.backend.exec_argv(command, interactive=True)
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_PIPE_LIMIT,
        )


class HostLane(Lane):
    """Commands run on the runtime host. Open/unmanaged posture only."""

    name = "host"

    def identity(self) -> str:
        return "host"

    @staticmethod
    def _env(extra: dict[str, str]) -> dict[str, str]:
        from olano.backends.interpreter_shims import with_interpreter_shims
        from olano.security.shell_guard import sanitized_shell_env

        env = with_interpreter_shims(sanitized_shell_env())
        env.update(extra)
        return env

    async def run_build(
        self, command: str, *, cwd: Path, env: dict[str, str], timeout: int, log_path: Path
    ) -> BuildResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with open(log_path, "wb") as log:
            log.write(f"[olano {_stamp()}] build: {command}\n".encode())
            log.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    "/bin/sh",
                    "-c",
                    command,
                    cwd=str(cwd),
                    env=self._env(env),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                return BuildResult(False, None, 0.0, error=str(exc))
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.CancelledError:
                # The runtime is shutting down mid-build: the build must not
                # outlive it.
                _kill_group(proc.pid, signal.SIGKILL)
                raise
            except TimeoutError:
                _kill_group(proc.pid)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), _KILL_AFTER)
                _kill_group(proc.pid, signal.SIGKILL)
                await proc.wait()
                log.write(f"\n[olano {_stamp()}] stopped: exceeded {timeout}s\n".encode())
                return BuildResult(
                    False,
                    proc.returncode,
                    time.monotonic() - started,
                    True,
                    f"build exceeded {timeout}s",
                )
        # Grandchildren left running in the build's session have no business
        # outliving it (a dev server started by mistake, a watcher).
        _kill_group(proc.pid, signal.SIGKILL)
        seconds = time.monotonic() - started
        if proc.returncode != 0:
            return BuildResult(
                False, proc.returncode, seconds, error=f"build exited with code {proc.returncode}"
            )
        return BuildResult(True, 0, seconds)

    async def spawn_keeper(
        self, spec: dict[str, Any], *, env: dict[str, str]
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            keeper_source(),
            MARKER,
            json.dumps(spec, separators=(",", ":")),
            cwd=str(spec.get("cwd") or Path.cwd()),
            env=self._env(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_PIPE_LIMIT,
        )


def _kill_group(pgid: int, sig: int = signal.SIGTERM) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def resolve_lane(agent: str) -> tuple[Lane | None, str | None]:
    """The lane ``agent``'s page commands run in now, or ``(None, reason)``."""
    from olano.pages import policy

    sandbox = policy.live_sandbox(agent)
    if sandbox is not None:
        return SandboxLane(sandbox), None
    blocker = policy.execution_blocker(agent)
    if blocker:
        return None, blocker
    return HostLane(), None
