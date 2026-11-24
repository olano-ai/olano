"""Messenger pairing flows (Signal, WhatsApp, WeChat).

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_pairing_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the pairing routes to ``app``."""
    # ── Per-platform pair / connect flows ─────────────────────────────────────
    # Mirrors `olano connect <platform>` for the dashboard.  Streams status
    # + QR events over SSE so a button on the connector form can drive pairing
    # without the user touching a terminal.

    # ── Signal guided-setup endpoints ────────────────────────────────────────
    # These drive the dashboard's Signal "Set up / Connect" wizard. register /
    # verify / profile use argv-based subprocess execution (NOT a shell string)
    # so the captcha token and codes are never written to the
    # .olano/shell_runs/ logs that /api/shell/stream persists.

    async def _run_signal_cli(args: list[str], timeout: float = 120.0) -> dict:
        """Run `signal-cli <args>` via argv (no shell). Returns rc/stdout/stderr.

        argv form guarantees the captcha token / verification code are passed as
        opaque process arguments and never interpolated into a logged command
        string.
        """
        import asyncio as _asyncio

        try:
            proc = await _asyncio.create_subprocess_exec(
                "signal-cli",
                *args,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=400,
                detail="signal-cli not found — install it (brew install signal-cli).",
            ) from exc
        try:
            out_b, err_b = await _asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="signal-cli timed out") from exc
        return {
            "rc": proc.returncode,
            "stdout": (out_b or b"").decode("utf-8", "replace"),
            "stderr": (err_b or b"").decode("utf-8", "replace"),
        }

    def _live_signal_adapter(account: str, agent: str | None = None):
        """Return a connected SignalAdapter for ``account`` if one is live.

        When a daemon is running (whether olano spawned it or it's external)
        it holds the signal-cli account data-dir lock — so register / verify /
        updateProfile must go through its JSON-RPC, never a second `signal-cli`
        process, which would deadlock on that lock (the wizard "hangs").
        """
        from olano.platforms.config import Platform

        resources = getattr(runtime, "_agent_resources", {}) or {}
        candidates: list = []
        if agent:
            candidates = (resources.get(agent) or {}).get("platforms") or []
        else:
            for res in resources.values():
                candidates += (res or {}).get("platforms") or []
        for adapter in candidates:
            if (
                getattr(adapter, "platform", None) == Platform.SIGNAL
                and getattr(adapter, "account", None) == account
                and getattr(adapter, "client", None) is not None
            ):
                return adapter
        return None

    @app.post("/api/connectors/signal/detect")
    async def signal_detect(_req: _api.SignalDetectRequest | None = None) -> dict:
        """Report whether signal-cli is installed and its version."""
        import shutil as _shutil

        path = _shutil.which("signal-cli")
        if not path:
            return {"installed": False, "version": None, "path": None}
        res = await _run_signal_cli(["--version"], timeout=15.0)
        version = (res["stdout"] or res["stderr"]).strip() or None
        return {"installed": True, "version": version, "path": path}

    @app.post("/api/connectors/signal/register")
    async def signal_register(req: _api.SignalRegisterRequest) -> dict:
        """Register the bot number. Surfaces the captcha requirement to the UI.

        The captcha token (when supplied) is passed as an argv element only —
        it is never echoed back or written to any log.
        """
        auth_layer.assert_admin()  # provisions a deployment-level Signal number
        if not req.account:
            raise HTTPException(status_code=400, detail="account is required")
        # A live daemon means the account is already registered AND holds the
        # data-dir lock — shelling out `register` would hang. Short-circuit so
        # the wizard advances straight to the profile/save steps.
        if _live_signal_adapter(req.account):
            return {
                "ok": False,
                "captcha_required": False,
                "already_registered": True,
                "message": "A signal-cli daemon is already running for this number — it's registered.",
            }
        args = ["-a", req.account, "register"]
        if req.captcha:
            args += ["--captcha", req.captcha]
        res = await _run_signal_cli(args)
        stderr = res["stderr"] or ""
        lowered = stderr.lower()
        captcha_required = "captcha" in lowered and res["rc"] != 0
        # "Account is already registered" is not a failure for our purposes —
        # the number is usable, so the wizard should skip verification and move
        # straight on (set up a profile / save) rather than dead-ending.
        already_registered = "already registered" in lowered
        # Scrub: never return the raw token; only the non-sensitive guidance.
        message = stderr.strip() or res["stdout"].strip()
        return {
            "ok": res["rc"] == 0,
            "captcha_required": captcha_required,
            "already_registered": already_registered,
            "message": message,
        }

    @app.post("/api/connectors/signal/verify")
    async def signal_verify(req: _api.SignalVerifyRequest) -> dict:
        """Verify the SMS/voice code for a freshly registered number."""
        auth_layer.assert_admin()
        if not req.account or not req.code:
            raise HTTPException(status_code=400, detail="account and code are required")
        # If a daemon is already live, the number is verified/connected — skip
        # (and avoid the data-dir-lock hang from a second signal-cli process).
        if _live_signal_adapter(req.account):
            return {"ok": True, "message": "Already connected — no verification needed."}
        res = await _run_signal_cli(["-a", req.account, "verify", req.code])
        return {
            "ok": res["rc"] == 0,
            "message": (res["stderr"] or res["stdout"]).strip(),
        }

    @app.post("/api/connectors/signal/profile")
    async def signal_profile(req: _api.SignalProfileRequest) -> dict:
        """Set the account's Signal profile name (often required to send).

        If a managed daemon for this account is live, go through its JSON-RPC
        ``updateProfile`` — shelling out to `signal-cli updateProfile` would
        deadlock on the data-dir lock the running daemon holds. Otherwise shell
        out directly (no daemon running yet).
        """
        if req.agent:
            auth_layer.assert_agent_admin(req.agent)
        else:
            auth_layer.assert_admin()
        if not req.account or not req.given_name:
            raise HTTPException(status_code=400, detail="account and given_name are required")

        # Prefer a live daemon's JSON-RPC — a running daemon (managed or external)
        # holds the data-dir lock, so a second `signal-cli updateProfile` would
        # hang. The JSON-RPC param is ``givenName`` (verified against signal-cli).
        adapter = _live_signal_adapter(req.account, req.agent)
        if adapter is not None:
            result = await adapter._rpc(
                "updateProfile", {"account": req.account, "givenName": req.given_name}
            )
            return {"ok": result is not None, "via": "daemon-rpc"}

        # No live daemon — safe to shell out directly.
        res = await _run_signal_cli(
            ["-a", req.account, "updateProfile", "--given-name", req.given_name]
        )
        return {
            "ok": res["rc"] == 0,
            "via": "cli",
            "message": (res["stderr"] or res["stdout"]).strip(),
        }

    def _whatsapp_channel_extra(cfg: Any) -> dict[str, Any]:
        """The ``extra`` block of an agent's ``platform:whatsapp`` connector.

        The two callers below used to walk ``cfg.platforms`` — a field
        ``AgentConfig`` does not have, so ``getattr(cfg, "platforms", None) or
        []`` iterated nothing and both silently resolved an EMPTY extra. That
        is invisible while ``bridge_port``/``session_path`` are left on Auto
        (``resolve_runtime_layout`` then derives the same per-agent defaults
        either way) and wrong the moment either is pinned: Pair streamed from
        the default port instead of the configured bridge, and Reset wiped the
        default directory while the real session survived. Connectors live
        under ``channels``.
        """
        for plat in getattr(cfg, "channels", None) or []:
            ptype = getattr(plat, "type", None) or (
                plat.get("type") if isinstance(plat, dict) else plat
            )
            if str(ptype) != "platform:whatsapp":
                continue
            raw_extra = (
                plat.get("extra") if isinstance(plat, dict) else getattr(plat, "extra", None)
            )
            return dict(raw_extra) if isinstance(raw_extra, dict) else {}
        return {}

    @app.post("/api/connectors/whatsapp/reset")
    async def whatsapp_reset(agent: str | None = None) -> dict:
        """Hard-reset a WhatsApp pairing — stops the live bridge, wipes the
        credential dir, optionally reboots the agent.

        Used by the dashboard's pair modal when WhatsApp has logged the
        device out and the bridge can't recover on its own.  Without this
        endpoint a user would have to ``rm -rf`` the session dir from a
        terminal, which is not acceptable in a UI we ship to humans.

        Returns a structured summary so the caller can decide what to do
        next (typically: re-open the pair SSE stream).
        """
        if not agent:
            raise HTTPException(status_code=400, detail="agent name is required")
        # Destructive connector mutation on a specific agent → admin-or-owner.
        auth_layer.assert_agent_admin(agent)

        import shutil as _shutil

        from olano.config.loader import load_agent_config
        from olano.platforms.config import Platform
        from olano.platforms.whatsapp import WhatsAppAdapter

        steps: list[str] = []
        errors: list[str] = []

        # 1. Stop the live bridge subprocess so the port + creds files are
        #    released cleanly before we delete anything.
        stopped_bridge = False
        resources = getattr(runtime, "_agent_resources", {}) or {}
        platforms = (resources.get(agent) or {}).get("platforms") or []
        for plat_adapter in platforms:
            if getattr(plat_adapter, "platform", None) == Platform.WHATSAPP:
                try:
                    await plat_adapter.disconnect()
                    stopped_bridge = True
                    steps.append("stopped live bridge")
                except Exception as exc:
                    errors.append(f"disconnect failed: {exc}")
                break

        # 2. Resolve the agent's session path even if it isn't booted.
        agent_extra: dict[str, Any] = {}
        try:
            cfg = await asyncio.to_thread(load_agent_config, agent)
            agent_extra = _whatsapp_channel_extra(cfg)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Agent '{agent}' not found") from exc
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not load agent config: {exc}"
            ) from exc

        try:
            _, session_path = WhatsAppAdapter.resolve_runtime_layout(agent, agent_extra)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not resolve session path: {exc}",
            ) from exc

        # 3. Wipe the credential directory.  rmtree(ignore_errors=True) so
        #    a stale lock file doesn't block recovery.
        def _wipe_session() -> None:
            if session_path.exists():
                _shutil.rmtree(session_path, ignore_errors=True)
            session_path.mkdir(parents=True, exist_ok=True)

        try:
            await asyncio.to_thread(_wipe_session)
            steps.append(f"cleared {session_path}")
        except Exception as exc:
            errors.append(f"rmtree failed: {exc}")

        # 4. Reboot the agent so its adapter respawns a bridge with the
        #    empty session.  The next /pair SSE stream will just OBSERVE
        #    that bridge (no spawn) and surface the fresh QR.
        restarted_agent = False
        if agent in runtime._runners or agent in runtime._agent_resources:
            try:
                await runtime._stop_agent(agent)
                await runtime._boot_agent(agent)
                restarted_agent = agent in runtime._runners
                if restarted_agent:
                    steps.append("restarted agent")
                else:
                    errors.append("agent did not come back up after restart")
            except Exception as exc:
                errors.append(f"agent restart failed: {exc}")

        return {
            "ok": not errors,
            "agent": agent,
            "session_path": str(session_path),
            "stopped_bridge": stopped_bridge,
            "restarted_agent": restarted_agent,
            "steps": steps,
            "errors": errors,
        }

    async def _pin_weixin_account(agent: str, account_id: str) -> bool:
        """Bind a just-linked WeChat login to *agent*'s weixin connector.

        The QR credential store is server-global, so with several agents (or
        several linked logins) a connector saved with blank fields cannot know
        which account it should be. The pairing flow is launched from ONE
        agent's form — record that intent by writing the minted account id into
        the entry's pin field, so each agent stays bound to the login its owner
        scanned and two agents never race for the same one. Returns False when
        the agent has no ``platform:weixin`` connector saved yet (the form may
        still be open, unsaved) — the blank-field fallback covers that case.
        """
        try:
            data = await _api._read_agent_config_for_update(agent)
        except FileNotFoundError:
            return False
        channels = data.get("channels") or []
        for i, entry in enumerate(channels):
            ch_type = entry if isinstance(entry, str) else str(entry.get("type") or "")
            if ch_type != "platform:weixin":
                continue
            if isinstance(entry, str):
                entry = {"type": entry}
            extra = dict(entry.get("extra") or {})
            # `account_id` is the canonical key and beats the `bot_id` alias in
            # the adapter's resolution — overwrite whichever one is in play.
            pin_key = "account_id" if str(extra.get("account_id") or "").strip() else "bot_id"
            if str(extra.get(pin_key) or "").strip() == account_id:
                return True  # already pinned to this login
            extra[pin_key] = account_id
            entry["extra"] = extra
            channels[i] = entry
            data["channels"] = channels
            await _api._write_agent_config_async(agent, data)
            _api.logger.info(
                "weixin pair: pinned account %s to agent '%s' (%s)",
                account_id,
                agent,
                pin_key,
            )
            return True
        return False

    @app.get("/api/connectors/weixin/pair")
    async def weixin_pair_stream(
        request: Request,
        timeout: int = 480,
        agent: str | None = None,
    ) -> Any:
        """SSE stream of the iLink QR-login flow, for the connector form's
        "Link WeChat account" button.

        There is no developer console for this connector: Tencent mints the
        credentials when a personal WeChat account scans the QR and confirms.
        On confirmation they are persisted server-side under
        ``$LIONCLAW_HOME/weixin/accounts/`` where the adapter (and a connector
        saved with blank credential fields) picks them up — the bot token is a
        live WeChat session credential and deliberately never crosses this
        stream; only the account_id does. When ``?agent=`` names the form's
        agent and that agent already has a ``platform:weixin`` connector, the
        confirmed account id is also pinned into its ``extra`` so multiple
        agents each keep their own login (``"pinned"`` in the done event).

        Events:
          * ``{"type": "qr", "qr": "<scan data>", "refresh": n}``
          * ``{"type": "status", "status": "wait|scanned"}``
          * ``{"type": "done", "ok": true, "account_id": "...", "pinned": bool}``
          * ``{"type": "done", "ok": false, "reason": "..."}``
        """
        # Connector-management action: admin-or-owner of the agent whose form
        # launched it, plain admin when unscoped (mirrors the WhatsApp stream).
        if agent:
            auth_layer.assert_agent_admin(agent)
        else:
            auth_layer.assert_admin()

        from starlette.responses import StreamingResponse as SR

        from olano.config.home import get_olano_home
        from olano.platforms.weixin import check_weixin_requirements, qr_login_events

        timeout = max(30, min(timeout, 900))

        async def _generate():
            def evt(obj: dict) -> str:
                return f"data: {json.dumps(obj)}\n\n"

            if not check_weixin_requirements():
                yield evt(
                    {
                        "type": "done",
                        "ok": False,
                        "reason": "Weixin support needs the aiohttp and cryptography packages.",
                    }
                )
                return
            try:
                async for event in qr_login_events(str(get_olano_home()), timeout_seconds=timeout):
                    if await request.is_disconnected():
                        return
                    kind = event.get("event")
                    if kind == "qr":
                        yield evt(
                            {
                                "type": "qr",
                                "qr": event.get("qr"),
                                "refresh": event.get("refresh", 0),
                            }
                        )
                    elif kind == "status":
                        yield evt({"type": "status", "status": event.get("status")})
                    elif kind == "confirmed":
                        account_id = str(event.get("account_id") or "")
                        pinned = False
                        if agent and account_id:
                            try:
                                pinned = await _pin_weixin_account(agent, account_id)
                            except Exception as exc:  # config write must not eat the link
                                _api.logger.warning(
                                    "weixin pair: could not pin %s to agent '%s': %s",
                                    account_id,
                                    agent,
                                    exc,
                                )
                        # token stays server-side — see docstring.
                        yield evt(
                            {
                                "type": "done",
                                "ok": True,
                                "account_id": account_id,
                                "pinned": pinned,
                            }
                        )
                        return
                    elif kind == "error":
                        yield evt({"type": "done", "ok": False, "reason": event.get("reason")})
                        return
            except Exception as exc:  # network hiccup, Tencent 5xx, …
                _api.logger.warning("weixin pair stream failed: %s", exc)
                yield evt({"type": "done", "ok": False, "reason": f"QR login failed: {exc}"})

        return SR(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/connectors/whatsapp/pair")
    async def whatsapp_pair_stream(
        request: Request,
        port: int = 3000,
        timeout: int = 180,
        agent: str | None = None,
    ) -> Any:
        """SSE stream of WhatsApp bridge status events for QR-pairing in the UI.

        Probes the bridge HTTP server on ``port``.  If nothing is listening,
        spawns the bundled bridge as a child process so the user can pair from
        the dashboard without touching a terminal.  When the runtime's own
        adapter is already running on that port, this endpoint just observes
        and streams its QR / status updates.

        ``?agent=<name>`` resolves bridge_port + session_path the same way
        the live adapter does (``WhatsAppAdapter.resolve_runtime_layout``).
        Without it the endpoint targets the legacy global session path,
        which a multi-tenant runtime will NOT read — your pairing would
        silently land in the wrong directory.

        Events:
          * ``{"type": "status", "status": "starting|connecting|qr|connected|logged_out", ...}``
          * ``{"type": "log", "line": "..."}``
          * ``{"type": "done", "ok": bool, "reason": "..."}``
        """
        # Spawns/observes the pairing bridge for a specific agent (or the global
        # session when unscoped) — a connector-management action: admin-or-owner
        # of the agent, admin for the global session.
        if agent:
            auth_layer.assert_agent_admin(agent)
        else:
            auth_layer.assert_admin()

        import shutil as _shutil
        import signal
        import subprocess as _subprocess
        import sys

        from starlette.responses import StreamingResponse as SR

        from olano.config.home import get_olano_home
        from olano.platforms.whatsapp import WhatsAppAdapter

        agent_extra: dict[str, Any] = {}
        if agent:
            try:
                from olano.config.loader import load_agent_config

                agent_cfg = await asyncio.to_thread(load_agent_config, agent)
                agent_extra = _whatsapp_channel_extra(agent_cfg)
                agent_port, _ = WhatsAppAdapter.resolve_runtime_layout(agent, agent_extra)
                # Only override the URL default (3000) so explicit `?port=`
                # callers still win — they're either pinning a specific
                # bridge or already know the resolved port.
                if port == 3000:
                    port = agent_port
            except Exception:
                # Fall through to legacy global behaviour — the SSE stream
                # will still surface bridge errors below.
                pass

        async def _http_get_health() -> dict | None:
            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{port}/health",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as resp:
                        if resp.status == 200:
                            return cast("dict[Any, Any] | None", await resp.json())
            except Exception:
                return None
            return None

        async def _generate():
            def evt(obj: dict) -> str:
                return f"data: {json.dumps(obj)}\n\n"

            spawned: _subprocess.Popen | None = None
            spawned_log: Any | None = None

            try:
                yield evt({"type": "status", "status": "starting", "port": port})

                if not _shutil.which("node"):
                    yield evt(
                        {
                            "type": "done",
                            "ok": False,
                            "reason": "Node.js not found. Run ./install.sh.",
                        }
                    )
                    return

                health = await _http_get_health()
                if health and health.get("status") == "connected":
                    yield evt(
                        {
                            "type": "status",
                            "status": "connected",
                            "jid": health.get("jid"),
                            "uptime": health.get("uptime"),
                        }
                    )
                    yield evt({"type": "done", "ok": True, "reason": "already_paired"})
                    return

                # WhatsApp logged this session out — the bridge will loop on
                # bad credentials forever and any attempt to spawn another
                # bridge on this port will instantly exit 1 ("Logged out by
                # WhatsApp. Delete the session dir and re-pair.").  Reset by
                # stopping the live adapter, wiping the session, and falling
                # through to the spawn path so the user gets a fresh QR.
                if health and health.get("status") == "logged_out":
                    yield evt(
                        {
                            "type": "log",
                            "line": "Bridge is logged-out by WhatsApp. Clearing stale session…",
                        }
                    )
                    target_session: Path | None = None
                    if agent:
                        try:
                            _, target_session = WhatsAppAdapter.resolve_runtime_layout(
                                agent, agent_extra
                            )
                        except Exception:
                            target_session = None
                    if target_session is None:
                        target_session = get_olano_home() / "platforms" / "whatsapp" / "session"

                    # Stop the live adapter's bridge subprocess so the new
                    # spawn can bind port %d.  Without this, the second
                    # bridge instantly exits 1 ("address already in use").
                    if agent and runtime is not None:
                        try:
                            resources = getattr(runtime, "_agent_resources", {}) or {}
                            platforms = (resources.get(agent) or {}).get("platforms") or []
                            from olano.platforms.config import Platform

                            for plat_adapter in platforms:
                                plat = getattr(plat_adapter, "platform", None)
                                if plat == Platform.WHATSAPP:
                                    await plat_adapter.disconnect()
                                    yield evt(
                                        {
                                            "type": "log",
                                            "line": f"Stopped live adapter for agent '{agent}'.",
                                        }
                                    )
                                    break
                        except Exception as exc:
                            yield evt(
                                {
                                    "type": "log",
                                    "line": f"Could not stop live adapter (continuing): {exc}",
                                }
                            )

                    try:
                        import shutil as _shutil2

                        def _clear_target(session_dir: Path) -> None:
                            if session_dir.exists():
                                _shutil2.rmtree(session_dir, ignore_errors=True)
                            session_dir.mkdir(parents=True, exist_ok=True)

                        await asyncio.to_thread(_clear_target, target_session)
                        yield evt(
                            {
                                "type": "log",
                                "line": f"Cleared session at {target_session}.",
                            }
                        )
                    except Exception as exc:
                        yield evt(
                            {
                                "type": "done",
                                "ok": False,
                                "reason": f"Failed to clear stale session: {exc}",
                            }
                        )
                        return

                    health = None

                # Spawn bridge if nothing is listening.
                if health is None:
                    script = Path(WhatsAppAdapter._default_bridge_script())
                    if not script.exists():
                        yield evt(
                            {
                                "type": "done",
                                "ok": False,
                                "reason": f"Bridge script not found at {script}",
                            }
                        )
                        return
                    bridge_dir = script.parent
                    if not (bridge_dir / "node_modules").exists():
                        yield evt(
                            {
                                "type": "log",
                                "line": "Installing bridge npm deps (first run only)…",
                            }
                        )
                        try:

                            def _run_npm() -> None:
                                _subprocess.run(
                                    ["npm", "install", "--no-audit", "--no-fund", "--silent"],
                                    cwd=str(bridge_dir),
                                    check=True,
                                    timeout=120,
                                )

                            await asyncio.to_thread(_run_npm)
                        except Exception as exc:
                            yield evt(
                                {
                                    "type": "done",
                                    "ok": False,
                                    "reason": f"npm install failed: {exc}",
                                }
                            )
                            return

                    if agent:
                        _, session_path = WhatsAppAdapter.resolve_runtime_layout(agent, agent_extra)
                        log_path = session_path.parent / "bridge.log"
                    else:
                        session_path = get_olano_home() / "platforms" / "whatsapp" / "session"
                        log_path = get_olano_home() / "platforms" / "whatsapp" / "bridge.log"
                    session_path.mkdir(parents=True, exist_ok=True)
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    # Handle is intentionally kept open for the lifetime of the
                    # spawned bridge subprocess (its stdout/stderr sink); a context
                    # manager would close it immediately and break bridge logging.
                    spawned_log = open(log_path, "a")  # noqa: SIM115

                    # Popen forks + execs (and setsid's) — a blocking syscall
                    # hop, so both spawn sites run it via to_thread. Reads
                    # spawned_log at call time so the respawn path picks up
                    # its reopened handle.
                    def _spawn_bridge() -> "_subprocess.Popen[bytes]":
                        return _subprocess.Popen(
                            [
                                "node",
                                str(script),
                                "--port",
                                str(port),
                                "--session",
                                str(session_path),
                                "--mode",
                                "pairing",
                            ],
                            cwd=str(bridge_dir),
                            stdout=spawned_log,
                            stderr=spawned_log,
                            preexec_fn=None if sys.platform == "win32" else os.setsid,
                        )

                    proc = await asyncio.to_thread(_spawn_bridge)
                    spawned = proc
                    yield evt(
                        {
                            "type": "log",
                            "line": f"Bridge spawned (pid={proc.pid}). Waiting for HTTP…",
                        }
                    )

                # Track where THIS bridge's log output starts so the
                # "Logged out by WhatsApp" recovery heuristic doesn't
                # match a stale line from an earlier subprocess.
                try:
                    spawn_log_offset = log_path.stat().st_size if spawned is not None else 0
                except OSError:
                    spawn_log_offset = 0

                # Poll /health
                started = asyncio.get_event_loop().time()
                last_status = ""

                def _log_since_spawn_contains(needle: str) -> bool:
                    """Was ``needle`` written to ``log_path`` after we spawned?"""
                    try:
                        with open(log_path, "rb") as fh:
                            fh.seek(spawn_log_offset)
                            new_bytes = fh.read()
                        return needle in new_bytes.decode("utf-8", "replace")
                    except Exception:
                        return False

                async def _wipe_session_and_respawn() -> "_subprocess.Popen | None":
                    nonlocal spawn_log_offset
                    """Clear logged-out creds and start a fresh bridge.

                    The bridge subprocess writes ``Logged out by WhatsApp``
                    and exits 1 on startup if WhatsApp invalidated the
                    session.  Auto-clear the creds dir and respawn so the
                    user gets a fresh QR instead of a useless exit code.
                    """
                    nonlocal spawned_log
                    try:
                        import shutil as _shutil3

                        def _clear_session() -> None:
                            if session_path.exists():
                                _shutil3.rmtree(session_path, ignore_errors=True)
                            session_path.mkdir(parents=True, exist_ok=True)

                        await asyncio.to_thread(_clear_session)
                        yield_evt = {
                            "type": "log",
                            "line": (
                                "Bridge reported logged-out — cleared stale "
                                f"session at {session_path} and respawning."
                            ),
                        }
                        # We can't ``yield`` from a nested coroutine; the
                        # caller relays this message.
                        nonlocal_buf.append(yield_evt)
                        # Reopen the log file so the previous handle's buffer
                        # doesn't shadow the new subprocess's first lines.
                        if spawned_log is not None:
                            try:
                                spawned_log.close()
                            except Exception:
                                pass
                        # Lifetime tied to the subprocess; closed in the
                        # outer ``finally`` of ``_generate``.
                        spawned_log = open(log_path, "a")  # noqa: SIM115
                        try:
                            spawn_log_offset = log_path.stat().st_size
                        except OSError:
                            spawn_log_offset = 0
                        new_proc = await asyncio.to_thread(_spawn_bridge)
                        nonlocal_buf.append(
                            {
                                "type": "log",
                                "line": f"Bridge respawned (pid={new_proc.pid}).",
                            }
                        )
                        return new_proc
                    except Exception as exc:
                        nonlocal_buf.append(
                            {
                                "type": "done",
                                "ok": False,
                                "reason": f"Failed to recover after logout: {exc}",
                            }
                        )
                        return None

                nonlocal_buf: list[dict] = []
                already_recovered = False

                while True:
                    if await request.is_disconnected():
                        break

                    # Drain any messages queued by the recovery helper.
                    while nonlocal_buf:
                        yield evt(nonlocal_buf.pop(0))

                    if spawned is not None and spawned.poll() is not None:
                        # If the bridge died because the session was logged
                        # out, wipe it and try ONCE more — that's the whole
                        # reason for this recovery dance.
                        logged_out = _log_since_spawn_contains("Logged out by WhatsApp")
                        if logged_out and not already_recovered:
                            already_recovered = True
                            spawned = await _wipe_session_and_respawn()
                            while nonlocal_buf:
                                yield evt(nonlocal_buf.pop(0))
                            if spawned is None:
                                return
                            started = asyncio.get_event_loop().time()
                            last_status = ""
                            await asyncio.sleep(1.0)
                            continue

                        reason = f"Bridge exited (code {spawned.returncode}). See {log_path}."
                        if logged_out:
                            reason += (
                                " The bridge could not recover the stale session — "
                                "delete it manually and click Pair again."
                            )
                        yield evt({"type": "done", "ok": False, "reason": reason})
                        return

                    health = await _http_get_health()
                    status = (health or {}).get("status", "starting")
                    qr = (health or {}).get("qr")
                    payload = {"type": "status", "status": status}
                    if qr:
                        payload["qr"] = qr
                    if (health or {}).get("jid"):
                        payload["jid"] = (health or {}).get("jid")
                    if status != last_status or qr:
                        yield evt(payload)
                        last_status = status

                    if status == "connected":
                        yield evt({"type": "done", "ok": True, "reason": "paired"})
                        return
                    if status == "logged_out":
                        if not already_recovered:
                            already_recovered = True
                            # Kill the live bridge before wiping/respawning.
                            if spawned is not None and spawned.poll() is None:
                                try:
                                    if sys.platform != "win32":
                                        os.killpg(os.getpgid(spawned.pid), signal.SIGTERM)
                                    else:
                                        spawned.terminate()
                                except Exception:
                                    pass
                            spawned = await _wipe_session_and_respawn()
                            while nonlocal_buf:
                                yield evt(nonlocal_buf.pop(0))
                            if spawned is None:
                                return
                            started = asyncio.get_event_loop().time()
                            last_status = ""
                            await asyncio.sleep(1.0)
                            continue
                        yield evt(
                            {
                                "type": "done",
                                "ok": False,
                                "reason": "WhatsApp logged this session out (already retried once).",
                            }
                        )
                        return

                    if (asyncio.get_event_loop().time() - started) > timeout:
                        yield evt(
                            {
                                "type": "done",
                                "ok": False,
                                "reason": f"Timed out after {timeout}s without pairing.",
                            }
                        )
                        return

                    await asyncio.sleep(1.5)

            finally:
                # Note: we INTENTIONALLY do not kill the spawned bridge here —
                # leaving it alive lets the runtime adapter pick it up via
                # /health when the agent is restarted.  The user can stop it
                # with `lsof -i :<port>` if needed, or it will be cleaned up
                # when the dashboard process exits.
                if spawned_log is not None:
                    try:
                        spawned_log.close()
                    except Exception:
                        pass

        return SR(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
