"""Agent, system and platform start/stop/restart.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException

from olano.config.agent_state import set_agent_paused
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_lifecycle_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the lifecycle routes to ``app``."""
    # ── Agent lifecycle (boot/stop/restart a single agent in a live runtime) ─
    # Bodies live in runtime/agent_lifecycle.py so the AgentFather's admin
    # tools can run the identical logic in-process — a cookie-less loopback
    # HTTP call 401s on multi-user installs (see that module's docstring).
    from olano.runtime import agent_lifecycle

    _boot_failure_http_exc = agent_lifecycle.boot_failure_http_exc

    @app.post("/api/agents/{name}/stop")
    async def stop_agent(name: str) -> dict:
        return await agent_lifecycle.stop_agent(runtime, name)

    @app.post("/api/agents/{name}/restart")
    async def restart_agent(name: str) -> dict:
        return await agent_lifecycle.restart_agent(runtime, name)

    @app.post("/api/agents/{name}/prompt/refresh")
    async def refresh_agent_prompt(name: str) -> dict[str, Any]:
        """Fingerprint-check one running agent's stable prompt sections against
        its live graph right now, hot-swapping a rebuilt graph on mismatch —
        the manual, immediate version of the background prompt refresh sweep.
        Returns ``{ok, rebuilt, changed: [...]}``; ``rebuilt: false`` with no
        ``changed`` entries means the running prompt is already current."""
        # Rebuilding is a management action on the agent (same posture as the
        # raw-config editor and restart).
        auth_layer.assert_agent_admin(name)
        try:
            await asyncio.to_thread(_api._read_agent_config, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        checker = getattr(runtime, "_prompt_refresh_check", None)
        if checker is None:
            raise HTTPException(
                status_code=501, detail="Prompt refresh not supported by this runtime"
            )
        if runtime.get_runner(name) is None:
            return {"ok": False, "rebuilt": False, "changed": [], "reason": "agent not running"}
        result: dict[str, Any] = await checker(name, push_event=_api._push_event)
        return result

    @app.post("/api/agents/{name}/reload-vault")
    async def reload_agent_vault(name: str) -> dict:
        """Re-read the credentials vault, re-hydrate env vars, and reboot the
        agent so refreshed ``${vault:...}`` references (e.g. a rotated API key)
        take effect. Mirrors the CLI ``/reload-vault`` command, exposed to the
        dashboard chat ``/reload-vault`` slash command."""
        from olano.vault.resolver import get_vault, set_vault
        from olano.vault.runtime import hydrate_env_from_vault, unlock_vault_for_session

        # Confirm the agent exists (and its config parses) before any vault work.
        def _validate() -> None:
            _api._agent_config_path(name)
            _api._read_agent_config(name)

        try:
            await asyncio.to_thread(_validate)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid agent.json5: {exc}") from exc

        # Resolve the vault — already unlocked this session, or auto-unlock via
        # env/keyring. We never prompt here (no interactive TTY in the runtime).
        vault = get_vault()
        if vault is None:
            try:
                result = unlock_vault_for_session(allow_prompt=False)
                if result and result.vault is not None:
                    set_vault(result.vault)
                    vault = result.vault
            except Exception:  # pragma: no cover - defensive
                vault = None
        if vault is None:
            raise HTTPException(
                status_code=503, detail="The vault is locked. Unlock it, then reload."
            )

        try:
            injected = hydrate_env_from_vault(vault, overwrite=True)
        except Exception:
            injected = []
        try:
            secret_count = len(vault.list_names())
        except Exception:
            secret_count = 0

        # Reboot so the rebuilt agent resolves the refreshed secrets/env. If the
        # agent isn't running we only refresh env — it'll pick up the rest on
        # its next start.
        rebooted = False
        if name in runtime._runners or name in runtime._agent_resources:
            try:
                await runtime._stop_agent(name)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Stop failed: {exc}") from exc
            await runtime._boot_agent(name)
            rebooted = name in runtime._runners
            if not rebooted:
                entry = runtime.registry.get(name) if hasattr(runtime, "registry") else None
                err_msg = (
                    entry.error if entry and entry.error else ""
                ) or "boot failed; see daemon logs"
                raise _boot_failure_http_exc(name, err_msg)
            set_agent_paused(name, False)
            _api._push_event("agent_started", {"name": name})
            _api._broadcast_roster_changed(name)

        return {
            "ok": True,
            "name": name,
            "secrets": secret_count,
            "env": len(injected),
            "rebooted": rebooted,
        }

    @app.post("/api/agents/{name}/start")
    async def start_agent(name: str) -> dict:
        return await agent_lifecycle.start_agent(runtime, name)

    # ── System lifecycle (start/stop/restart all agents at once) ─────────────

    @app.post("/api/system/stop")
    async def system_stop() -> dict:
        names = list(runtime._runners.keys())
        errors: list[str] = []
        for name in names:
            try:
                await runtime._stop_agent(name)
                set_agent_paused(name, True)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        return {"ok": not errors, "stopped": len(names) - len(errors), "errors": errors}

    @app.post("/api/system/start")
    async def system_start() -> dict:
        from olano.config.loader import list_agents

        all_names = list_agents()
        # Explicit "start all" clears every persisted pause.
        for name in all_names:
            set_agent_paused(name, False)
        running = set(runtime._runners.keys())
        to_start = [n for n in all_names if n not in running]
        errors: list[str] = []
        started: list[str] = []
        for name in to_start:
            try:
                await runtime._boot_agent(name)
                if name in runtime._runners:
                    started.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        return {"ok": not errors, "started": started, "errors": errors}

    @app.post("/api/system/restart")
    async def system_restart() -> dict:
        names = list(runtime._runners.keys())
        errors: list[str] = []
        restarted: list[str] = []
        for name in names:
            try:
                await runtime._stop_agent(name)
                await runtime._boot_agent(name)
                if name in runtime._runners:
                    restarted.append(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        return {"ok": not errors, "restarted": restarted, "errors": errors}

    # ── Platform lifecycle (the OS process itself, not just the agents) ───────
    # Same controller as the owner-only /olano chat command.

    @app.get("/api/platform/status")
    async def platform_status() -> dict:
        from olano.platform_control import get_platform_controller

        controller = get_platform_controller()
        info = controller.status()
        info["dashboard_reachable"] = await controller.dashboard_reachable()
        return info

    @app.post("/api/platform/restart")
    async def platform_restart() -> dict:
        from olano.platform_control import get_platform_controller

        strategy = get_platform_controller().schedule_restart()
        if strategy.startswith("busy-"):
            return {"ok": False, "strategy": strategy, "detail": "A lifecycle action is in flight."}
        return {"ok": True, "strategy": strategy}

    @app.post("/api/platform/shutdown")
    async def platform_shutdown() -> dict:
        from olano.platform_control import get_platform_controller

        strategy = get_platform_controller().schedule_shutdown()
        if strategy == "unsupported":
            return {
                "ok": False,
                "strategy": strategy,
                "detail": (
                    "Cannot stop a docker deployment from inside the container — "
                    "run `docker stop <container>` on the host."
                ),
            }
        if strategy.startswith("busy-"):
            return {"ok": False, "strategy": strategy, "detail": "A lifecycle action is in flight."}
        return {"ok": True, "strategy": strategy}

    @app.post("/api/platform/update")
    async def platform_update() -> dict:
        import asyncio as _asyncio

        from olano.config.home import get_olano_home
        from olano.platform_control import get_platform_controller
        from olano.update.strategies import check_update

        controller = get_platform_controller()
        if controller.update_in_progress():
            return {"ok": False, "detail": "An update is already in progress."}
        check = await _asyncio.to_thread(check_update, get_olano_home())
        if not check.can_apply or not check.update_available:
            return {"ok": False, "detail": check.details}
        started = controller.begin_update()
        return {"ok": started, "detail": check.details}
