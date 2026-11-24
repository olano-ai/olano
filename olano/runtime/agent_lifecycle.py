"""Single-agent lifecycle operations (start / stop / restart) for a live runtime.

These are the bodies of ``POST /api/agents/{name}/start|stop|restart``, hosted
outside the ``create_api`` closures so there are TWO ways to reach the one
implementation:

* the dashboard routes in :mod:`olano.runtime.api` (thin ``await`` wrappers),
* :mod:`olano.admin.lifecycle`, which dispatches them directly onto the
  runtime loop when the AgentFather's tools run inside ``olano serve``.

The second path exists because the loopback HTTP hop the admin tools used to
make carries no browser session, and on multi-user installs the cookie-auth
gate (``runtime/auth.py:access_gate``) 401s it — ``olano_start_agent``
failed with a bare ``RuntimeError: auth_required`` while the dashboard's Start
button worked. In-process dispatch removes the auth question entirely for the
runtime-resident caller; the pause flag, boot-error translation, SSE events,
and roster broadcasts stay one implementation either way.

Failures are raised as :class:`fastapi.HTTPException` (plain-string or
structured ``detail``) so the routes re-raise them unchanged and in-process
callers translate ``detail`` exactly like the HTTP client path does.

Helpers that stayed in ``api.py`` (config read, SSE broadcast, secret-error
heuristics) are imported lazily via :func:`_api` — never at module scope — so
importing this module stays light and free of import cycles.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from olano.config.agent_state import set_agent_paused


def _api() -> Any:
    """The loaded :mod:`olano.runtime.api` module (always present at call
    time: both callers run inside a process that has built the API)."""
    from olano.runtime import api

    return api


def _validate_agent_config(name: str) -> None:
    # Confirm the YAML exists (and silently migrate legacy keys/backend) before booting.
    api = _api()
    try:
        api._agent_config_path(name)
        api._read_agent_config(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    except Exception as exc:  # malformed YAML, etc.
        raise HTTPException(status_code=400, detail=f"Invalid agent.json5: {exc}") from exc


async def _boot_capturing_errors(runtime: Any, name: str) -> list[str]:
    # _boot_agent swallows exceptions internally and parks the failure on
    # the registry entry — but if the failure happens BEFORE the entry is
    # registered (config load / validation), the registry stays empty.
    # Capture the most recent ERROR record from the runtime logger as a
    # fallback so the toast shows the real cause either way.
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR and "boot" in record.getMessage().lower():
                captured.append(record.getMessage())

    handler = _Capture()
    runtime_logger = logging.getLogger("olano.runtime.server")
    runtime_logger.addHandler(handler)
    try:
        await runtime._boot_agent(name)
    finally:
        runtime_logger.removeHandler(handler)
    return captured


def _boot_error_message(runtime: Any, name: str, captured: list[str]) -> str:
    entry = runtime.registry.get(name) if hasattr(runtime, "registry") else None
    if entry and entry.error:
        return str(entry.error)
    if captured:
        # Strip the "Failed to boot agent 'name': " prefix so the message reads cleanly.
        msg = captured[-1]
        prefix = f"Failed to boot agent '{name}': "
        return msg[len(prefix) :] if msg.startswith(prefix) else msg
    return "boot failed; see daemon logs"


def boot_failure_http_exc(name: str, err_msg: str) -> HTTPException:
    """Translate a raw agent-boot error into an actionable HTTP error.

    When the failure looks like a missing credential, return a *structured*
    ``detail`` the dashboard can turn into an "unlock vault & retry" prompt:

        {"code": "vault_locked" | "missing_secret",
         "message": "<human readable>",
         "secret": "DEEPSEEK_API_KEY" | None,
         "vault_locked": bool,
         "raw": "<original error>"}

    Everything else falls through to a plain string detail (status 500) so
    the behaviour is unchanged for non-credential failures.
    """
    api = _api()
    secret = api._extract_secret_name(err_msg)
    if not (secret or api._looks_like_secret_error(err_msg)):
        return HTTPException(status_code=500, detail=err_msg)

    # Determine whether the vault can be opened right now. An env-var /
    # keyring auto-unlock counts as "unlocked" — in that case the secret is
    # simply absent rather than locked away.
    from olano.vault.resolver import get_vault, set_vault
    from olano.vault.runtime import unlock_vault_for_session

    vault = get_vault()
    if vault is None:
        try:
            result = unlock_vault_for_session(allow_prompt=False)
            if result and result.vault is not None:
                set_vault(result.vault)
                vault = result.vault
        except Exception:  # pragma: no cover - defensive
            vault = None
    vault_locked = vault is None

    if vault_locked:
        message = (
            f"{name} needs the secret {secret} to start, but your vault is locked."
            if secret
            else f"{name} couldn't start — it needs a credential from your locked vault."
        )
        return HTTPException(
            status_code=503,
            detail={
                "code": "vault_locked",
                "message": message,
                "secret": secret,
                "vault_locked": True,
                "raw": err_msg,
            },
        )

    message = (
        f"{name} needs the secret {secret}, which isn't in your vault or environment yet."
        if secret
        else f"{name} is missing a required credential. Add it in the Vault, then start the agent."
    )
    return HTTPException(
        status_code=422,
        detail={
            "code": "missing_secret",
            "message": message,
            "secret": secret,
            "vault_locked": False,
            "raw": err_msg,
        },
    )


def agent_unavailable_http_exc(runtime: Any, name: str) -> HTTPException:
    """Why a chat call found no live runner for ``name``, as an HTTP error.

    Every chat route used to answer a missing runner with ``404 Agent '<name>'
    not found``, which is also what a stopped agent got: the phone showed a
    normal "Say hello" screen, took the message, and then said the agent did
    not exist. A missing runner has five causes and only one is "no such
    agent", so this tells them apart with the same rules as the roster
    (``GET /api/agents``), and the two can never disagree:

    * no config on disk: ``404`` with the old text (a genuinely unknown name);
    * still booting: ``503`` ``agent_starting`` (worth retrying in a moment);
    * stopped on purpose (the dashboard's Stop): ``409`` ``agent_not_running``,
      ``state: "paused"``;
    * failed to boot, or held back by the plan's agent limit: ``409`` with
      ``state: "error"`` / ``"blocked"`` and the ``reason``.

    ``can_start`` says whether this caller may turn it back on (an admin; the
    start route is admin-only), so a client offers the button only to them.
    ``message`` is a complete sentence, which is what older clients show.
    """
    from olano.config.agent_state import is_agent_paused
    from olano.config.loader import find_agent_config_path

    if find_agent_config_path(name) is None:
        return HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    display = name
    try:
        display = str(_api()._read_agent_config_cached(name).get("display_name") or name)
    except Exception:  # the name is enough for the sentence
        pass

    entry = runtime.registry.get(name) if hasattr(runtime, "registry") else None
    status = str(getattr(entry, "status", "") or "")
    error = str(getattr(entry, "error", "") or "")
    blocked_reason = str(getattr(runtime, "_blocked_agents", {}).get(name, "") or "")

    if status == "starting":
        return HTTPException(
            status_code=503,
            detail={
                "code": "agent_starting",
                "state": "starting",
                "message": f"{display} is starting. Try again in a moment.",
            },
        )

    if is_agent_paused(name):
        state, reason = "paused", ""
        message = f"{display} is stopped. It won't reply until an admin starts it again."
    elif blocked_reason:
        state, reason = "blocked", blocked_reason
        message = f"{display} is held back by this deployment's plan, so it can't reply."
    elif status == "error" or error:
        state, reason = "error", error
        message = f"{display} couldn't start, so it can't reply right now."
    else:
        state, reason = "stopped", ""
        message = f"{display} isn't running, so it can't reply right now."

    from olano.runtime.auth import current_user

    user = current_user()
    can_start = (user is None or bool(user.is_admin)) and state != "blocked"
    return HTTPException(
        status_code=409,
        detail={
            "code": "agent_not_running",
            "state": state,
            "message": message,
            "reason": reason,
            "can_start": can_start,
        },
    )


async def start_agent(runtime: Any, name: str) -> dict:
    """Start (boot) one agent; the body of ``POST /api/agents/{name}/start``."""
    api = _api()
    # Explicit start clears the persisted pause so it boots on next restart.
    set_agent_paused(name, False)
    # Already booted — no-op.
    if name in runtime._runners:
        return {"ok": True, "name": name, "status": "running", "already_running": True}
    # Already booting — answer now instead of parking this request behind a
    # boot that can take minutes (model build + MCP + RAG ingest). The
    # runtime serializes boots per agent, so the agent still ends up running
    # exactly once; a second click / a second dashboard just gets told so.
    # (`is True` / callable: the runtime is duck-typed in tests.)
    _in_flight = getattr(runtime, "agent_boot_in_flight", None)
    if callable(_in_flight) and _in_flight(name) is True:
        return {"ok": True, "name": name, "status": "starting", "already_starting": True}

    # The json5 parse inside can cost ~0.6s on a large config — off the loop.
    await asyncio.to_thread(_validate_agent_config, name)
    captured = await _boot_capturing_errors(runtime, name)

    if name in runtime._runners:
        api._push_event("agent_started", {"name": name})
        api._broadcast_roster_changed(name)
        return {"ok": True, "name": name, "status": "running"}

    raise boot_failure_http_exc(name, _boot_error_message(runtime, name, captured))


async def stop_agent(runtime: Any, name: str) -> dict:
    """Stop (pause) one agent; the body of ``POST /api/agents/{name}/stop``."""
    api = _api()
    # Persist the pause so the agent stays offline across server restarts.
    set_agent_paused(name, True)
    if name not in runtime._runners and name not in runtime._agent_resources:
        return {"ok": True, "name": name, "status": "stopped", "already_stopped": True}
    try:
        await runtime._stop_agent(name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Stop failed: {exc}") from exc
    api._push_event("agent_stopped", {"name": name})
    api._broadcast_roster_changed(name)
    return {"ok": True, "name": name, "status": "stopped"}


async def restart_agent(runtime: Any, name: str) -> dict:
    """Restart one agent; the body of ``POST /api/agents/{name}/restart``."""
    api = _api()
    if name in runtime._runners or name in runtime._agent_resources:
        try:
            await runtime._stop_agent(name)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Stop failed: {exc}") from exc

    # Start path mirrors /start: validate YAML, capture the real boot error.
    await asyncio.to_thread(_validate_agent_config, name)
    captured = await _boot_capturing_errors(runtime, name)

    if name in runtime._runners:
        # An explicit restart means "I want this running" — clear any pause.
        set_agent_paused(name, False)
        api._push_event("agent_started", {"name": name})
        api._broadcast_roster_changed(name)
        return {"ok": True, "name": name, "status": "running", "restarted": True}

    raise boot_failure_http_exc(name, _boot_error_message(runtime, name, captured))
