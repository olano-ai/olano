"""Route an in-process tool's subprocess through the agent's exec sandbox.

Some in-process (Tier-1) tools shell out to a CLI: `safe_shell`, `node_run`,
`video_to_text` (ffmpeg). On a managed deployment those must run inside the
isolated Docker sandbox (same source-/secret-free boundary as the agent's
`execute`), NOT on the runtime host. `sandboxed_run(argv)` does exactly that:
when the current agent has a live sandbox it runs the command via `docker exec`
(through the backend); on an open/unmanaged box with no sandbox it runs locally
via `subprocess.run`, a drop-in for the common
`subprocess.run(argv, capture_output=True, text=True)` shape.

The host branch is gated on posture, and the gate fails closed. The runtime
container holds the product source (`/app`) and the credential vault, so a
managed box with no live sandbox gets a refusal (`EXIT_REFUSED`, see
`host_execution_refused`), never a host process: the same rule
`core/builder.py` applies when it withholds the host shell and
`admin/agent_exec.py` applies to cross-agent commands. The graph path is
already covered by the security middleware's shell chokepoint
(`deepagents_middleware._run_shell_command`), which intercepts every shell-
family tool call before the tool body runs; this is the belt for the tool
bodies themselves and for every in-process CLI that comes through here.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olano.backends.interpreter_shims import with_interpreter_shims

logger = logging.getLogger(__name__)


#: Exit code of a run that never started because host execution is forbidden
#: on this box. 126 is POSIX "command found but cannot execute", which is what
#: it is; it also cannot collide with a real command's exit status in a way
#: that reads as success. Callers that want the reason print ``stderr``.
EXIT_REFUSED = 126

HOST_EXECUTION_REFUSED = (
    "Managed deployment: commands run only inside this agent's isolated sandbox, "
    "and none is live right now. Nothing runs in the runtime container here. "
    "Restart the agent to rebuild its sandbox, or ask the operator to enable the "
    "sandbox execution backend for this deployment."
)


@dataclass
class ExecResult:
    """subprocess.CompletedProcess-compatible result (stdout/stderr/returncode)."""

    stdout: str
    stderr: str
    returncode: int


def host_execution_refused(result: ExecResult) -> bool:
    """True when ``result`` is the posture refusal, not a command's own failure."""
    return result.returncode == EXIT_REFUSED and result.stderr == HOST_EXECUTION_REFUSED


def _host_execution_forbidden() -> bool:
    """True when this box must not run agent commands in the runtime container.

    Fails closed: an unresolvable posture means no host execution. Mirrors
    ``enforcement.require_sandboxed_exec`` (which already fails closed on its
    own) one level up, so an import error in the enforcement layer gets the
    same answer as a resolver error inside it.
    """
    try:
        from olano.deployment import enforcement as dep_enforcement

        return bool(dep_enforcement.require_sandboxed_exec())
    except Exception:
        logger.exception("posture resolution failed; refusing host execution (fail closed)")
        return True


def _live_sandbox_for(agent: str | None) -> Any | None:
    """``agent``'s live sandbox backend, or None (open posture / no sandbox).

    A **sidecar** counts: `builder._start_sidecar_sandbox` gives a non-execute
    agent the same container beside its filesystem backend, and its shell still
    runs as uid 1000 over the same bind-mounted workspace. `sandbox_is_backend`
    is False there, which is why nothing here asks it.
    """
    try:
        from olano.core.builder import get_sandbox_backend
        from olano.security.deepagents_middleware import _is_live_sandbox_backend

        backend = get_sandbox_backend(agent)
        return backend if _is_live_sandbox_backend(backend) else None
    except Exception:
        return None


def _live_sandbox_for_current_agent() -> Any | None:
    """The current turn's live sandbox backend, or None (open posture / no sandbox)."""
    try:
        from olano.tools.talk_to import current_agent

        return _live_sandbox_for(current_agent.get())
    except Exception:
        return None


def sandboxed_run(argv: Sequence[str], *, timeout: int = 60, cwd: str | None = None) -> ExecResult:
    """Run ``argv`` in the agent's sandbox when one is live, else on the host.

    ``argv`` is a list (never a shell string): on the host it is passed to
    subprocess without a shell; in the sandbox it is `shlex.join`-ed into the
    `docker exec` command. Callers use ``.stdout`` / ``.stderr`` / ``.returncode``
    exactly as with ``subprocess.run(..., capture_output=True, text=True)``.

    ``cwd`` is a host path. The sandbox mounts the workspace at its host path
    (one path namespace), so the same string is valid on both lanes.

    The host lane exists for the open/unmanaged posture only. On a managed box
    with no live sandbox the command does not run and the result is the
    refusal (``returncode == EXIT_REFUSED``, ``stderr`` the reason); callers
    that already handle a failed command handle it unchanged, and
    :func:`host_execution_refused` tells the two apart. Raising would have
    changed the contract for every subprocess-shaped caller.
    """
    args = [str(a) for a in argv]
    backend = _live_sandbox_for_current_agent()
    if backend is not None:
        # `run()` (not deepagents' `execute()`) — callers below expect stdout and
        # stderr apart, the way subprocess.run hands them over.
        command = shlex.join(args)
        if cwd:
            command = f"cd {shlex.quote(str(cwd))} && {command}"
        res = backend.run(command, timeout=timeout)
        return ExecResult(str(res.stdout), str(res.stderr), int(res.returncode))
    if _host_execution_forbidden():
        logger.warning(
            "refusing host execution of %r: managed posture with no live sandbox", args[:1]
        )
        return ExecResult("", HOST_EXECUTION_REFUSED, EXIT_REFUSED)
    # Host branch: the environment with platform secrets stripped (the
    # vault key must never reach a child a tool spawns), and the
    # runtime-written interpreter shims appended to PATH so `python`
    # resolves here too.
    from olano.security.shell_guard import sanitized_shell_env

    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd or None,
        env=with_interpreter_shims(sanitized_shell_env()),
    )
    return ExecResult(proc.stdout or "", proc.stderr or "", proc.returncode)


def sandbox_active_for_current_agent() -> bool:
    """True when the current agent's commands are being routed into a sandbox."""
    return _live_sandbox_for_current_agent() is not None


def sandbox_active_for(agent: str | None) -> bool:
    """True when ``agent``'s commands are being routed into a sandbox."""
    return _live_sandbox_for(agent) is not None


def handoff_to_sandbox(
    path: Path | str,
    *,
    created: Iterable[Path | str] = (),
    agent: str | None = None,
) -> None:
    """Give a file the runtime just wrote to the sandboxed agent (uid 1000).

    The sandbox agent is uid/gid 1000 and a bind mount preserves host ownership,
    so anything the *runtime process* writes into a workspace lands root-owned:
    the agent that asked for the chart can read it and cannot overwrite, move or
    delete it. This hands it over.

    ``created`` names directories this write had to make — a file the agent can
    write inside a directory it cannot is only half a hand-off, and every writer
    below ``mkdir(parents=True)``s. ``agent`` defaults to the turn's agent from
    the contextvar; **pass it explicitly from any surface that runs outside**
    ``agent_runtime_context`` — the dashboard's file endpoints and a chat
    attachment save both do, and there the contextvar is unset, so the default
    would quietly do nothing at exactly the places the Files view writes.

    A no-op without a live sandbox: on the host lane the runtime and the agent
    are the same user, and re-owning a file to a uid that means nothing there
    would be a permission change nobody asked for. Never raises.

    Deliberately NOT for: host paths outside a workspace (a magic link editing
    ``/opt/olano/olano.env`` — that file belongs to the runtime), ``/tmp``
    scratch (never shared with the container), and ``agents/<name>/`` avatars and
    sprites (outside the workspace, served over HTTP, never opened by a shell).
    """
    live = _live_sandbox_for(agent) if agent else _live_sandbox_for_current_agent()
    if live is None:
        return
    try:
        from olano.core.builder import chown_for_sandbox

        for extra in created:
            chown_for_sandbox(Path(extra))
        chown_for_sandbox(Path(path))
    except Exception:  # pragma: no cover - chown_for_sandbox already swallows
        logger.debug("sandbox hand-off skipped for %s", path, exc_info=True)


def _live_sandbox_holding(path: Path) -> Any | None:
    """The live sandbox whose bind-mounted workspace contains ``path``, or None."""
    try:
        from olano.core.builder import _SANDBOX_BACKENDS
        from olano.security.deepagents_middleware import _extract_live_sandbox

        resolved = path.resolve()
        for backend in list(_SANDBOX_BACKENDS.values()):
            live = _extract_live_sandbox(backend)
            root = getattr(live, "workspace_root", None)
            if root is not None and resolved.is_relative_to(Path(root).resolve()):
                return live
    except Exception:
        logger.debug("could not resolve the sandbox holding %s", path, exc_info=True)
    return None


def handoff_workspace_write(path: Path | str, *, created: Iterable[Path | str] = ()) -> None:
    """Hand ``path`` to the sandbox agent when it lies inside a live sandbox's workspace.

    The default for a runtime write: a file in an agent's workspace belongs to
    the agent, and this needs no caller to know which agent or whether a sandbox
    is even up. ``atomic_write_text`` calls it on every write, so a writer that
    goes through it never needs :func:`handoff_to_sandbox`. A no-op for paths
    outside every live workspace (the vault, config.yaml, the home) and on a box
    with no sandbox. Never raises.
    """
    path = Path(path)
    if _live_sandbox_holding(path) is None:
        return
    try:
        from olano.core.builder import chown_for_sandbox

        for extra in created:
            chown_for_sandbox(Path(extra))
        chown_for_sandbox(path)
    except Exception:  # pragma: no cover - chown_for_sandbox already swallows
        logger.debug("workspace hand-off skipped for %s", path, exc_info=True)


def missing_parents(path: Path | str) -> list[Path]:
    """The ancestors of ``path`` that do not exist yet, outermost first.

    Call this **before** the ``mkdir(parents=True)`` and pass the result as
    ``created=`` — after the fact there is no way to tell which directories this
    write made from the ones that were already there.
    """
    out: list[Path] = []
    cur = Path(path).parent
    while not cur.exists() and cur != cur.parent:
        out.append(cur)
        cur = cur.parent
    return list(reversed(out))
