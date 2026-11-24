"""Cross-agent command execution for the admin (AgentFather) tool layer.

The cross-agent file tools (:mod:`olano.admin.workspace_files`) cover
inspect / seed / fix; this module adds the missing verb — **run** — under the
same containment rules, so a setup script, migration, verifier, or cleanup can
actually be *executed* in a target agent's workspace and produce a real
receipt (exit code + output) instead of a prediction of what it would do.

Executor selection mirrors the backend build rules in ``core/builder.py`` and
is never weaker than what the target itself gets:

* **Target has a live docker sandbox** → the command runs via ``docker exec``
  in *that* container (``OlanoDockerBackend.run``): the target's mounts,
  uid-1000 user, resource caps, and no-source/no-secrets boundary all apply.
  Host paths resolve in-container thanks to the one-path-namespace mounts.
* **Managed posture, no live sandbox** → refuse with an actionable error
  (start the agent first). Commands NEVER fall back to the runtime host on a
  managed box — that container holds the product source and the vault.
* **Open posture (self-host, e.g. tc-manual-host installs)** → ``sh -c`` on
  the host with the working directory pinned inside the target's workspace.
  That is the same host shell the target's own ``local_shell`` backend is,
  so it is not an escalation there. The child env is the target's: process
  env + the target's ``config.env`` + its resolved vault credentials — the
  exact environment contract the target's own stdio-MCP children get
  (:func:`olano.core.mcp_setup.agent_secret_env`). The managed sandbox
  path deliberately injects nothing: that container's no-secrets boundary is
  the point, and the target's own shell has no secrets there either.

Approval is enforced at the tool layer: ``olano_run_agent_command`` is the
only caller with model exposure and sits in the always-on
``cross_agent_exec`` HITL category — every command is approved by a human
individually (remembered "always" rules and scheduled-turn blanket approvals
deliberately do not apply; see ``core/runner.py``).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from typing import Any

from olano.admin.workspace_files import _resolve_inside, _workspace_root

logger = logging.getLogger(__name__)

# Per-stream cap: the result is an execution receipt read back into a model
# turn, not a data lane (mirrors the workspace_files read cap in spirit).
_OUTPUT_CAP = 64 * 1024
_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600
_EXIT_TIMEOUT = 124  # sh/timeout(1) convention


def _cap_stream(text: str) -> tuple[str, bool]:
    if len(text) <= _OUTPUT_CAP:
        return text, False
    return (
        text[:_OUTPUT_CAP] + f"\n… [truncated at {_OUTPUT_CAP} chars]",
        True,
    )


def _as_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _live_sandbox(agent: str) -> Any | None:
    """The target's live sandbox backend, or None (open posture / not booted)."""
    try:
        from olano.core.builder import get_sandbox_backend
        from olano.security.deepagents_middleware import _is_live_sandbox_backend

        backend = get_sandbox_backend(agent)
        return backend if _is_live_sandbox_backend(backend) else None
    except Exception:
        return None


def _managed_posture() -> bool:
    """True when agents must not run raw shell in the runtime container.

    Fails closed: an unresolvable posture means no host execution, the same
    rule ``core/builder.py`` applies when it withholds the host shell.
    """
    try:
        from olano.deployment import enforcement as dep_enforcement

        return dep_enforcement.require_sandboxed_exec()
    except Exception:
        logger.exception("posture resolution failed; refusing host execution (fail closed)")
        return True


def _target_host_env(agent: str) -> dict[str, str]:
    """Process env + the target's ``config.env`` + its resolved vault credentials.

    The same per-agent env contract the target's stdio-MCP children get. Env
    provisioning is best-effort — a locked vault degrades to process env plus
    the literal ``config.env`` values, and a command that needed a secret
    fails visibly in the receipt rather than silently running as the wrong
    identity.
    """
    env = dict(os.environ)
    cfg_env: dict[str, str] = {}
    try:
        from olano.config.loader import load_agent_config

        cfg_env = {str(k): str(v) for k, v in (load_agent_config(agent).env or {}).items()}
    except Exception:
        logger.debug("could not load config.env for %s", agent, exc_info=True)
    try:
        from olano.core.mcp_setup import agent_secret_env

        env.update(agent_secret_env(cfg_env or None, agent))
    except Exception:
        logger.debug("vault credentials unavailable for %s; using config.env only", agent)
        env.update(cfg_env)
    return env


def _sandbox_unavailable_hint(stderr: str, exit_code: int) -> str | None:
    if exit_code == 0:
        return None
    lowered = stderr.lower()
    if (
        "is not running" in lowered
        or "no such container" in lowered
        or "no sandbox container" in lowered
    ):
        return (
            "The target's sandbox container is not running — start the agent "
            "(olano_start_agent) and retry."
        )
    return None


def run_agent_command(
    agent: str,
    command: str,
    *,
    cwd: str = "",
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run one shell command inside *agent*'s workspace and return the receipt.

    ``cwd`` is resolved against the target's workspace root and containment-
    checked exactly like the cross-agent file tools (traversal and symlink
    escapes are refused). The returned dict is the execution receipt: it
    always carries ``exit_code``, ``stdout``, ``stderr``, ``executor``
    (``"sandbox"`` or ``"host"``), and ``duration_seconds``.
    """
    command = (command or "").strip()
    if not command:
        raise ValueError("Command is empty.")

    root = _workspace_root(agent)
    target_cwd = _resolve_inside(root, cwd)

    from olano.security.shell_guard import validate_command

    safe, reason = validate_command(command)
    if not safe:
        raise PermissionError(reason)

    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, _MAX_TIMEOUT))

    backend = _live_sandbox(agent)
    note: str | None = None
    started = time.monotonic()

    if backend is not None:
        # One path namespace: the workspace is mounted at its host path inside
        # the container, so the containment-checked host cwd is valid there.
        wrapped = f"cd {shlex.quote(str(target_cwd))} && ({command})"
        res = backend.run(wrapped, timeout=timeout)
        stdout, stderr, exit_code = _as_text(res.stdout), _as_text(res.stderr), int(res.returncode)
        executor = "sandbox"
        note = _sandbox_unavailable_hint(stderr, exit_code)
    else:
        if _managed_posture():
            raise RuntimeError(
                f"Managed deployment: commands run only inside the target agent's isolated "
                f"sandbox, and '{agent}' has none live right now. Start the agent "
                f"(olano_start_agent) and retry — commands never fall back to the "
                f"runtime host here."
            )
        if not target_cwd.is_dir():
            raise FileNotFoundError(
                f"'{target_cwd}' does not exist — the workspace is provisioned at the "
                f"agent's first boot. Start the agent, or seed files first with "
                f"olano_write_agent_file."
            )
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(target_cwd),
                env=_target_host_env(agent),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            stdout, stderr, exit_code = proc.stdout or "", proc.stderr or "", proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = _as_text(exc.stdout)
            stderr = (_as_text(exc.stderr) + f"\nCommand timed out after {timeout}s").strip()
            exit_code = _EXIT_TIMEOUT
        executor = "host"

    duration = round(time.monotonic() - started, 3)
    stdout, stdout_truncated = _cap_stream(stdout)
    stderr, stderr_truncated = _cap_stream(stderr)
    receipt: dict[str, Any] = {
        "agent": agent,
        "command": command,
        "cwd": str(target_cwd),
        "executor": executor,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "duration_seconds": duration,
    }
    if note:
        receipt["note"] = note
    return receipt
