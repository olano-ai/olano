"""Run one credentialed command in a throwaway container the agent has no shell in.

The posture (ADR-002): a credential never exists inside the agent's persistent
sandbox, not even transiently. The one accepted exception is an **ephemeral job
container** — exactly what sandboxed stdio MCP servers use today
(:func:`olano.core.mcp_setup.plan_stdio_launch`): a ``docker run --rm --init``
from the sandbox image, the workspace bind-mounted at its host path, secrets
forwarded as **name-only** ``-e NAME`` flags whose values live only in the
docker CLI child's process env (never on an argv), the runtime's ``os.environ``
never merged in, dead after the command. A private ``git clone``/``push`` must
produce files in the sandbox filesystem, so no in-process tool can replace it —
hence :func:`run_connected_job`, which ``tools/git_tools.py`` builds on.

Three lanes, decided by the same policy seam every ephemeral launch asks
(:func:`olano.core.mcp_setup.ephemeral_launch_mode`, fail-closed):

- ``sandboxed`` — the job container described above.
- ``host`` — open/unmanaged posture with no docker: the command runs on the
  operator's own box with the secrets in the child env only (mirrors the host
  branch of ``plan_stdio_launch``).
- ``blocked`` — exit 126 with the reason; nothing runs.

Whatever the lane, every secret *value* is scrubbed from the captured output
before it is returned, and this module never raises.
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from olano.tools._sandbox_exec import ExecResult

logger = logging.getLogger(__name__)

# Shell conventions a model reading the transcript already knows: GNU
# `timeout` exits 124, a command that cannot be executed 126, one that is not
# found 127.
EXIT_TIMEOUT = 124
EXIT_BLOCKED = 126
EXIT_UNAVAILABLE = 127

REDACTED = "[redacted]"

# Wall-clock bound on the best-effort `docker rm -f` after a job timeout.
_RM_TIMEOUT = 30

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-.") or "job"


def scrub_secrets(text: str, values: Iterable[str]) -> str:
    """Replace every non-empty secret value in *text* with ``[redacted]``.

    Longest value first, so a secret that is a prefix of another can never
    leave the longer one's tail behind.
    """
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def _job_path_export() -> str:
    """PATH line for a job: the image's own PATH, interpreter shims appended.

    Deliberately NOT :func:`interpreter_shims.shell_path_export`, which
    *prepends* the shared tool layer. That layer is written by the agent from
    its persistent sandbox, so with it first on PATH a planted
    ``/opt/agent-tools/bin/git`` would run inside the job — with the
    credential in its env. The image's binaries win here; the runtime-written
    shims only fill a name the image lacks.
    """
    from olano.backends.interpreter_shims import CONTAINER_SHIM_PATH

    return f"export PATH=$PATH:{shlex.quote(CONTAINER_SHIM_PATH)}"


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run(
    cmd: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str],
    timeout: int,
    label: str,
    on_timeout: object | None = None,
) -> ExecResult:
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if callable(on_timeout):
            on_timeout()
        stderr = _as_text(exc.stderr).rstrip()
        note = f"[job timeout] {label} exceeded {timeout}s and was stopped"
        return ExecResult(
            _as_text(exc.stdout), f"{stderr}\n{note}" if stderr else note, EXIT_TIMEOUT
        )
    except FileNotFoundError as exc:
        return ExecResult("", f"[job error] {label}: {exc}", EXIT_UNAVAILABLE)
    return ExecResult(proc.stdout or "", proc.stderr or "", proc.returncode)


def _run_on_host(
    argv: list[str],
    *,
    workspace_root: Path,
    env_overlay: Mapping[str, str],
    timeout: int,
    label: str,
) -> ExecResult:
    """Open/unmanaged posture: the operator's own box, secrets in the child env only."""
    from olano.backends.interpreter_shims import with_interpreter_shims
    from olano.security.shell_guard import sanitized_shell_env

    env = {**with_interpreter_shims(sanitized_shell_env()), **env_overlay}
    return _run(argv, cwd=workspace_root, env=env, timeout=timeout, label=label)


def _run_in_container(
    argv: list[str],
    *,
    agent: str,
    workspace_root: Path,
    env_overlay: Mapping[str, str],
    timeout: int,
    label: str,
) -> ExecResult:
    """Managed posture: a throwaway hardened container from the sandbox image."""
    from olano.backends.lionclaw_docker import ephemeral_run_args
    from olano.core import mcp_setup

    docker_cli = shutil.which("docker")
    if not docker_cli:
        return ExecResult(
            "", f"[job error] {label}: the docker CLI is not available", EXIT_UNAVAILABLE
        )
    mcp_setup._prepare_stdio_sandbox_paths(agent, workspace_root)
    name = f"olano-job-{_slug(label)}-{_slug(agent)}-{uuid.uuid4().hex[:8]}"
    run_args = ephemeral_run_args(
        label=label,
        agent_name=agent,
        workspace_root=workspace_root,
        secret_names=list(env_overlay),
        image=mcp_setup._stdio_sandbox_image(),
        interactive=False,
        extra_mounts_ro=True,
        container_name=name,
    )
    # run_args[0] is the bare "docker" token; the spawn uses the absolute CLI
    # path because the child env is the minimal allowlist, not a login
    # environment with a trustworthy PATH.
    cmd = [docker_cli, *run_args[1:], "sh", "-c", f"{_job_path_export()}; exec {shlex.join(argv)}"]
    cli_env = {**mcp_setup._minimal_cli_env(), **env_overlay}

    def _remove_container() -> None:
        # subprocess killed the CLI child with SIGKILL, which docker cannot
        # sig-proxy — the container would otherwise outlive the job.
        try:
            subprocess.run(
                [docker_cli, "rm", "-f", name],
                env=mcp_setup._minimal_cli_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_RM_TIMEOUT,
            )
        except Exception:
            logger.debug("job %s: could not remove timed-out container %s", label, name)

    return _run(
        cmd, cwd=None, env=cli_env, timeout=timeout, label=label, on_timeout=_remove_container
    )


def run_connected_job(
    argv: Sequence[str],
    *,
    agent: str,
    workspace_root: Path,
    secret_env: Mapping[str, str],
    extra_env: Mapping[str, str] | None = None,
    timeout: int = 600,
    label: str,
) -> ExecResult:
    """Run *argv* once with *secret_env* reachable only by that process.

    ``argv`` is a list, never a shell string. ``secret_env`` values are
    delivered through the process environment (name-only ``-e`` flags in the
    container lane) and scrubbed from the returned stdout/stderr; ``extra_env``
    is non-secret configuration delivered the same way. ``label`` names the
    job family (``olano.job=<label>`` on the container). Never raises: a
    blocked posture returns exit 126, a timeout 124, a missing executable 127.
    """
    args = [str(a) for a in argv]
    secrets = {k: v for k, v in dict(secret_env).items() if k and v}
    overlay: dict[str, str] = {**{k: v for k, v in dict(extra_env or {}).items() if k}, **secrets}
    values = list(secrets.values())

    try:
        from olano.core import mcp_setup

        mode, reason = mcp_setup.ephemeral_launch_mode()
    except Exception:
        logger.debug("job %s: launch-mode resolution failed", label, exc_info=True)
        mode, reason = "blocked", "the deployment's execution posture could not be resolved"

    try:
        if mode == "host":
            result = _run_on_host(
                args,
                workspace_root=workspace_root,
                env_overlay=overlay,
                timeout=timeout,
                label=label,
            )
        elif mode == "sandboxed":
            result = _run_in_container(
                args,
                agent=agent,
                workspace_root=workspace_root,
                env_overlay=overlay,
                timeout=timeout,
                label=label,
            )
        else:
            result = ExecResult(
                "", f"[job blocked] {reason or 'the execution posture is unresolved'}", EXIT_BLOCKED
            )
    except Exception as exc:
        logger.debug("job %s failed to launch", label, exc_info=True)
        result = ExecResult("", f"[job error] {label}: {exc}", EXIT_UNAVAILABLE)

    return ExecResult(
        scrub_secrets(result.stdout, values),
        scrub_secrets(result.stderr, values),
        result.returncode,
    )
