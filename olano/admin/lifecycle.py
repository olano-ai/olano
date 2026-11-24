"""Agent lifecycle control for the admin (AgentFather) tool layer.

Start, stop (pause), and restart agents by running the SAME lifecycle bodies
the dashboard's Start/Stop buttons use (:mod:`olano.runtime.agent_lifecycle`,
the extracted handlers of ``POST /api/agents/{name}/start|stop|restart``), so
there is exactly one lifecycle behavior everywhere: the persisted pause flag,
boot-error translation (vault-locked / missing-secret hints), SSE events, and
roster broadcasts all come from that one implementation.

How the bodies are reached depends on where this code runs:

* **In-process** — inside ``olano serve`` (the AgentFather's normal home)
  the lifecycle coroutine is dispatched straight onto the runtime loop via
  ``run_coroutine_threadsafe``, the same worker-thread bridge as
  ``notification_tools._deliver_sync``. No HTTP, no auth: a loopback HTTP call
  carries no browser session, so multi-user cookie auth
  (``runtime/auth.py:access_gate``) 401s it — the bug that made
  ``olano_start_agent`` fail with a bare ``RuntimeError: auth_required``.
* **Separate process, daemon up** — probe by pidfile and POST the endpoint
  over loopback. On multi-user installs this path still 401s (there is no
  session to send); the error now says so actionably instead of leaking the
  bare gate code.
* **No runtime at all** — start/stop persist the pause flag so the choice
  takes effect on the next runtime boot, reported honestly as
  ``applied: "next_boot"``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Coroutine
from typing import Any, NoReturn

from olano.config.agent_state import is_agent_paused, set_agent_paused

# Boot can legitimately take a while (model build, MCP servers); stop is quick.
_START_TIMEOUT = 180.0
_STOP_TIMEOUT = 60.0


def _api_reachable() -> bool:
    from olano.admin.diagnostics import _get_runtime

    if _get_runtime() is not None:
        return True  # we ARE the runtime process; its API serves on the loop
    try:
        from olano.runtime import daemon

        pid = daemon.read_pid()
        return bool(pid and daemon.is_pid_alive(pid))
    except Exception:
        return False


def _inproc_runtime() -> tuple[Any, asyncio.AbstractEventLoop] | None:
    """The live in-process ``RuntimeServer`` and its loop, or ``None``.

    Both must be real: duck-typed runtimes in tests (``MagicMock`` /
    ``object()``) carry no usable loop and fall through to the HTTP path.
    """
    from olano.admin.diagnostics import _get_runtime

    runtime = _get_runtime()
    loop = getattr(runtime, "_loop", None)
    if runtime is not None and isinstance(loop, asyncio.AbstractEventLoop) and loop.is_running():
        return runtime, loop
    return None


def _raise_lifecycle_detail(detail: Any, fallback: str, cause: BaseException) -> NoReturn:
    """One translation of an endpoint-shaped failure ``detail`` for BOTH paths
    (in-process ``HTTPException.detail`` and the HTTP body's ``detail``), so
    the tool surfaces identical messages either way."""
    if isinstance(detail, dict):  # structured boot failure (vault_locked / missing_secret)
        raise RuntimeError(detail.get("message") or json.dumps(detail)) from cause
    raise RuntimeError(str(detail or fallback)) from cause


def _run_inproc(
    coro_factory: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    timeout: float,
    loop: asyncio.AbstractEventLoop,
) -> dict[str, Any]:
    """Run a lifecycle coroutine on the runtime loop from a sync tool context.

    Sync admin tools execute in executor threads, where blocking on
    ``run_coroutine_threadsafe`` is safe. Called on the runtime loop itself
    (an async caller), blocking would deadlock the very loop the coroutine
    needs — schedule it and say so instead.
    """
    try:
        running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        loop.create_task(coro_factory())
        return {"ok": True, "status": "scheduled", "detail": "Scheduled on the runtime loop."}

    from fastapi import HTTPException

    future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
    try:
        result = future.result(timeout=timeout)
    except TimeoutError as exc:
        # The operation keeps running on the loop — this mirrors an HTTP
        # client timeout against the endpoint, not a cancellation.
        raise RuntimeError(
            f"timed out after {timeout:.0f}s waiting for the runtime lifecycle operation "
            "(it may still complete in the background)"
        ) from exc
    except HTTPException as exc:
        _raise_lifecycle_detail(
            exc.detail, f"lifecycle operation failed (HTTP {exc.status_code})", exc
        )
    return result if isinstance(result, dict) else {"ok": True, "response": result}


def _api_post_json(path: str, timeout: float) -> dict[str, Any]:
    """POST the local runtime API; raises RuntimeError with the server's own
    error detail (e.g. the vault-locked hint from a failed boot)."""
    import urllib.error
    import urllib.request

    from olano.runtime import daemon

    port = daemon._runtime_api_port()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    token = os.environ.get("LIONCLAW_DASHBOARD_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers, data=b"{}", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return payload if isinstance(payload, dict) else {"ok": True, "response": payload}
    except urllib.error.HTTPError as exc:
        detail: Any = None
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("detail")
        except Exception:
            pass
        if detail in ("auth_required", "setup_required"):
            # The multi-user cookie gate (runtime/auth.py access_gate): a
            # loopback POST carries no browser session. Inside `olano
            # serve` lifecycle dispatches in-process and never gets here —
            # reaching this means a separate process is talking to a daemon.
            hint = (
                "first-run setup hasn't been completed — open the dashboard and "
                "create the admin account first"
                if detail == "setup_required"
                else "the runtime is in multi-user mode and its API requires a "
                "signed-in dashboard session, which a loopback call doesn't have"
            )
            raise RuntimeError(
                f"the runtime refused the request ({detail}): {hint}. "
                "Use the dashboard's agent Start/Stop buttons, or run this "
                "command inside the runtime process."
            ) from exc
        _raise_lifecycle_detail(detail, f"runtime API answered HTTP {exc.code}", exc)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"could not reach the runtime API: {exc}") from exc


def require_agent(name: str) -> None:
    """Raise ``ValueError`` unless ``name`` names an agent that exists on disk.

    Public because every admin module that takes an agent name needs the same
    check; ``_require_agent`` stays as the in-module alias this file already uses.
    """
    from olano.config.home import get_olano_paths

    if not (get_olano_paths().agents_dir / name / "agent.json5").is_file():
        raise ValueError(f"Agent '{name}' does not exist.")


#: Historical private spelling, kept so existing call sites read unchanged.
_require_agent = require_agent


def start_agent(name: str) -> dict[str, Any]:
    """Start (boot) an agent and clear its persisted pause flag.

    With no runtime up, the cleared flag alone means the agent boots when the
    runtime next starts.
    """
    _require_agent(name)
    inproc = _inproc_runtime()
    if inproc is not None:
        runtime, loop = inproc
        from olano.runtime import agent_lifecycle

        result = _run_inproc(
            lambda: agent_lifecycle.start_agent(runtime, name), _START_TIMEOUT, loop
        )
        result.setdefault("name", name)
        result["applied"] = "now"
        return result
    if not _api_reachable():
        set_agent_paused(name, False)
        return {
            "ok": True,
            "name": name,
            "status": "will_start",
            "applied": "next_boot",
            "detail": "Runtime is not running; pause cleared so the agent boots with it.",
        }
    result = _api_post_json(f"/api/agents/{name}/start", timeout=_START_TIMEOUT)
    result.setdefault("name", name)
    result["applied"] = "now"
    return result


def stop_agent(name: str) -> dict[str, Any]:
    """Stop (pause) an agent — it stays offline across runtime restarts.

    The pause is persisted state, not config: ``start_agent`` (or the
    dashboard's Start) clears it.
    """
    _require_agent(name)
    inproc = _inproc_runtime()
    if inproc is not None:
        runtime, loop = inproc
        from olano.runtime import agent_lifecycle

        result = _run_inproc(lambda: agent_lifecycle.stop_agent(runtime, name), _STOP_TIMEOUT, loop)
        result.setdefault("name", name)
        result["applied"] = "now"
        return result
    if not _api_reachable():
        set_agent_paused(name, True)
        return {
            "ok": True,
            "name": name,
            "status": "paused",
            "applied": "next_boot",
            "detail": "Runtime is not running; pause persisted so the agent stays offline.",
        }
    result = _api_post_json(f"/api/agents/{name}/stop", timeout=_STOP_TIMEOUT)
    result.setdefault("name", name)
    result["applied"] = "now"
    return result


def restart_agent(name: str) -> dict[str, Any]:
    """Restart an agent (stop + fresh boot) — the way config changes apply.

    Requires a live runtime: with none, there is nothing to restart — check
    ``paused`` state and use start/stop instead.
    """
    _require_agent(name)
    inproc = _inproc_runtime()
    if inproc is not None:
        runtime, loop = inproc
        from olano.runtime import agent_lifecycle

        result = _run_inproc(
            lambda: agent_lifecycle.restart_agent(runtime, name), _START_TIMEOUT, loop
        )
        result.setdefault("name", name)
        result["applied"] = "now"
        return result
    if not _api_reachable():
        raise RuntimeError(
            "Runtime is not running — nothing to restart. "
            f"(Agent '{name}' is {'paused' if is_agent_paused(name) else 'not paused'}; "
            "it will boot with the runtime unless paused.)"
        )
    result = _api_post_json(f"/api/agents/{name}/restart", timeout=_START_TIMEOUT)
    result.setdefault("name", name)
    result["applied"] = "now"
    return result
